"""PS5 Pro stock checker: Amazon.de, PlayStation Store DE, MediaMarkt.de, Saturn.de,
Alternate.de, expert.de
Requires: pip install curl_cffi
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OXYLABS_USER, OXYLABS_PASS
Optional: MAX_PRICE (default 900), STORE_GROUP (free | proxy | all)
Restock history is appended to restocks.csv (committed by the workflow).
"""
import csv
import html
import json
import os
import random
import re
import urllib.parse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests

# ---------------------------------------------------------------- config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OXY_USER = os.environ["OXYLABS_USER"].strip().strip("\"'")
OXY_PASS = os.environ["OXYLABS_PASS"].strip().strip("\"'")

MAX_PRICE = float(os.environ.get("MAX_PRICE", "900"))  # MSRP 899.99

AMAZON_URL = "https://www.amazon.de/dp/B0FRYBZR91"
# Amazon's standard add-to-cart link: opens a page where you confirm adding it
AMAZON_CART_URL = "https://www.amazon.de/gp/aws/cart/add.html?ASIN.1=B0FRYBZR91&Quantity.1=1"
CART_LINKS = {"Amazon.de": AMAZON_CART_URL}
MEDIAMARKT_URL = "https://www.mediamarkt.de/de/product/_sony-playstationr5-pro-2-tb-konsole-3018022.html"
# Saturn belongs to MediaMarkt and uses the same product number and page layout:
SATURN_URL = "https://www.saturn.de/de/product/_sony-playstationr5-pro-2-tb-konsole-3018022.html"

ALTERNATE_URL = "https://www.alternate.de/Sony-Interactive-Entertainment/PlayStation-5-Pro-2-TB-E-Chassis-Spielkonsole/html/product/100160951"
EXPERT_URL = "https://www.expert.de/shop/unsere-produkte/gaming-freizeit/sony-playstation/playstation-konsolen/11364201744-playstation-5-pro.html"
# expert loads stock via this API (store chosen by the website for you):
EXPERT_API_URL = "https://production.brntgs.expert.de/api/pricepds?webcode=11364201744&storeId=e_2879130"

TZ = ZoneInfo("Europe/Vilnius")
# Warn on Telegram when a store has failed for this long (and at least 3 runs)
FAIL_WARN_MIN = int(os.environ.get("FAIL_WARN_MIN", "30"))
HISTORY_FILE = "restocks.csv"
# Which stores this run checks: "free" (no proxy), "proxy" or "all"
STORE_GROUP = (os.environ.get("STORE_GROUP") or "all").strip().lower()
SUMMARY_HOUR = 8  # daily report is sent by the first proxy/all run after 08:00

# schema.org availability values that mean "you can order it now"
BUYABLE = {"InStock", "LimitedAvailability", "OnlineOnly", "PreOrder", "PreSale", "BackOrder"}
# Sanity check: the page must really be about the PS5 Pro
EXPECTED_NAME = re.compile(r"(?i)playstation\W*5\W*pro")

# Sony shop (PS Direct moved to playstation.com/de-de/store/hardware in Oct 2026).
# Set the GitHub variable PSDIRECT_URL to EITHER the PS5 Pro product page in
# Sony's store OR a JSON stock request URL copied from DevTools.
PSDIRECT_DEFAULT = "https://www.playstation.com/de-de/ps5/ps5-pro/buy-now/"
PSDIRECT_URL = (os.environ.get("PSDIRECT_URL") or "").strip().strip("\"'").strip()
if not PSDIRECT_URL.startswith("https://"):
    PSDIRECT_URL = PSDIRECT_DEFAULT
# Link sent in the Telegram alert:
PSDIRECT_PAGE = os.environ.get("PSDIRECT_PAGE") or PSDIRECT_URL


# ---------------------------------------------------------------- helpers ---
PROXY_BYTES = [0]  # uncompressed bytes received through the proxy this run


def count_bytes(s, n):
    if getattr(s, "via_proxy", False):
        PROXY_BYTES[0] += n


def make_session(use_proxy=True):
    """New sticky residential IP per call (different IP per store)."""
    if not use_proxy:
        s = requests.Session(impersonate="chrome", timeout=25)
        s.headers.update({"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        s.via_proxy = False
        return s
    sid = random.randint(1_000_000_000, 9_999_999_999)
    pw = urllib.parse.quote(OXY_PASS, safe="")
    proxy = (f"http://customer-{OXY_USER}-cc-de-sessid-{sid}-sesstime-10:"
             f"{pw}@pr.oxylabs.io:7777")
    s = requests.Session(impersonate="chrome", timeout=25,
                         proxies={"http": proxy, "https": proxy})
    s.headers.update({"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
    s.via_proxy = True
    return s


def parse_eur(text):
    """'1.099,99 €' -> 1099.99"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d.,]*", str(text))
    if not m:
        return None
    num = m.group(0)
    if "," in num:
        num = num.replace(".", "").replace(",", ".")
    try:
        return float(num)
    except ValueError:
        return None


DEBUG = os.environ.get("DEBUG") == "true"


def page_info(name, r):
    """Save the page in debug mode and return a short description."""
    if DEBUG:
        os.makedirs("debug", exist_ok=True)
        fn = re.sub(r"[^a-z0-9]+", "_", name.lower()) + ".html"
        with open(os.path.join("debug", fn), "w", encoding="utf-8") as f:
            if isinstance(r, Partial):
                f.write("<!-- PARTIAL PAGE: download stopped after the stock data to save "
                        "proxy traffic, so it looks blank in a browser. -->\n")
            f.write(r.text)
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    title = html.unescape(m.group(1)).strip()[:70] if m else "no title"
    return f"HTTP {r.status_code}, {len(r.text)} bytes, title='{title}'"


class Partial:
    """Minimal response object for a partially downloaded page."""
    def __init__(self, status_code, text, headers, url):
        self.status_code, self.text, self.headers, self.url = status_code, text, headers, url


def fetch_until(s, url, done, max_bytes):
    """Download only until done(text) is True or max_bytes is reached.

    Saves proxy traffic: the stock info sits near the top of the page.
    """
    r = s.get(url, stream=True)
    buf = b""
    try:
        for chunk in r.iter_content():
            buf += chunk
            if len(buf) >= max_bytes:
                break
            if len(buf) > 8192 and done(buf.decode("utf-8", "ignore")):
                break
    finally:
        r.close()
    count_bytes(s, len(buf))
    return Partial(r.status_code, buf.decode("utf-8", "ignore"), r.headers, r.url)


def amazon_done(t):
    # Product bullets come right after the buy box / seller / price area.
    return 'id="feature-bullets"' in t or 'id="featurebullets"' in t


def mediamarkt_done(t):
    i = t.find("ld+json")
    return i >= 0 and t.find("</script>", i) > 0


def send_telegram(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


def alert(store, price, link, last_restock=None):
    p = f"€{price:.2f}" if price else "unknown"
    last = f"\n<b>Last restock here:</b> {last_restock}" if last_restock else ""
    send_telegram(
        f"🚨 <b>PS5 Pro IN STOCK!</b> 🚨\n"
        f"<b>Store:</b> {html.escape(store)}\n"
        f"<b>Price:</b> {p}{last}\n"
        f"<a href=\"{html.escape(link)}\">Buy now</a>"
        + "".join(f"  |  <a href=\"{html.escape(u)}\">🛒 Add to cart</a>"
                  for k, u in CART_LINKS.items() if store.startswith(k))
    )


def sold_out_alert(store, minutes):
    send_telegram(f"❌ <b>{html.escape(store)}</b>: sold out again "
                  f"(was in stock for about {minutes} min)")


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M")


def minutes_between(a, b):
    fmt = "%Y-%m-%d %H:%M"
    return int((datetime.strptime(b, fmt) - datetime.strptime(a, fmt)).total_seconds() // 60)


def log_history(ts, store, event, price, minutes=""):
    new = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time_vilnius", "store", "event", "price_eur", "minutes_in_stock"])
        w.writerow([ts, store, event, f"{price:.2f}" if price else "", minutes])


# ----------------------------------------------------------------- checks ---
# Each returns (status, price, note); status: IN_STOCK | OUT | BLOCKED | ERROR

def check_amazon(s):
    r = fetch_until(s, AMAZON_URL, amazon_done, max_bytes=900_000)
    t = r.text
    info = page_info("amazon", r)
    if r.status_code != 200 or "captcha" in t.lower():
        return "BLOCKED", None, f"captcha/blocked; {info}"

    title = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
    if not title or not EXPECTED_NAME.search(html.unescape(title.group(1))):
        return "ERROR", None, f"not the PS5 Pro page; {info}"
    if 'id="buybox"' not in t and 'id="desktop_buybox"' not in t:
        return "ERROR", None, f"page layout changed (no buy box area); {info}"
    if not amazon_done(t):
        return "ERROR", None, f"page incomplete or layout changed; {info}"

    has_cart = 'id="add-to-cart-button"' in t or 'id="buy-now-button"' in t

    i = t.find('id="merchantInfoFeature_feature_div"')
    seller_block = t[i:i + 2500] if i >= 0 else ""
    sold_by_amazon = "Amazon" in seller_block

    price = None
    j = t.find('id="corePrice_feature_div"')
    if j >= 0:
        m = re.search(r'class="a-offscreen">([^<]+)<', t[j:j + 3000])
        price = parse_eur(m.group(1)) if m else None

    if has_cart and sold_by_amazon:
        return "IN_STOCK", price, "sold by Amazon"
    return "OUT", price, f"cart={has_cart} amazon_seller={sold_by_amazon}; {info}"


def parse_jsonld(text):
    """Read schema.org availability/price of the page's MAIN product.

    MediaMarkt wraps it as BuyAction -> object (ProductGroup) and also lists
    other consoles under hasVariant; those are ignored on purpose.
    """
    blocks = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S)
    for b in blocks:
        try:
            data = json.loads(b)
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data])
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("@type") == "BuyAction":
                item = item.get("object") or {}
            if item.get("@type") not in ("Product", "ProductGroup"):
                continue
            offers = item.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if offers.get("@type") == "AggregateOffer" and "offers" in offers:
                inner = offers["offers"]
                offers = inner[0] if isinstance(inner, list) and inner else offers
            avail = str(offers.get("availability", ""))
            if not avail:
                continue
            price = parse_eur(offers.get("price") or offers.get("lowPrice"))
            short = avail.rsplit("/", 1)[-1]
            full_name = str(item.get("name", "")).replace("®", "").strip()
            name = full_name[:40]
            if not EXPECTED_NAME.search(full_name):
                return "ERROR", price, f"unexpected product on page: '{name}'"
            status = "IN_STOCK" if short in BUYABLE else "OUT"
            return status, price, f"{short} [{name}]"
    return None


def check_psdirect(s):
    if not PSDIRECT_URL.startswith("https://"):
        return "ERROR", None, f"bad PSDIRECT_URL: {PSDIRECT_URL!r}"
    r = s.get(PSDIRECT_URL, headers={"Referer": "https://www.playstation.com/de-de/"})
    count_bytes(s, len(r.content))
    info = page_info("playstation", r)
    if r.status_code != 200:
        return "BLOCKED", None, f"HTTP {r.status_code} (final URL: {r.url})"

    # Case 1: JSON stock request copied from DevTools
    if "json" in r.headers.get("content-type", ""):
        data = r.json()
        products = data.get("products") or [data]
        p = products[0] if products else {}
        stock = ((p.get("stock") or {}).get("stockLevelStatus")
                 or p.get("stockStatus") or p.get("availability") or "")
        price = parse_eur((p.get("price") or {}).get("value")
                          if isinstance(p.get("price"), dict) else p.get("price"))
        in_stock = str(stock).lower().replace("_", "") in ("instock", "available")
        return ("IN_STOCK" if in_stock else "OUT"), price, f"stock={stock}"

    # Case 2: product page
    res = parse_jsonld(r.text)
    if res:
        return res
    return "ERROR", None, f"no stock data on page; {info}"


def check_mms(s, url, label):
    """MediaMarkt and Saturn share the same shop system."""
    r = fetch_until(s, url, mediamarkt_done, max_bytes=300_000)
    info = page_info(label, r)
    if r.status_code != 200:
        return "BLOCKED", None, info
    res = parse_jsonld(r.text)
    if res:
        return res
    return "ERROR", None, f"no product JSON-LD found; {info}"


def jsonld_offer_done(t):
    """Stop once a complete JSON-LD block with offers has arrived."""
    for m in re.finditer(r'ld\+json[^>]*>(.*?)</script>', t, re.S):
        if '"offers"' in m.group(1):
            return True
    return False


def check_jsonld_shop(s, url, label):
    """Generic check for shops that publish schema.org Product data."""
    r = fetch_until(s, url, jsonld_offer_done, max_bytes=1_500_000)
    info = page_info(label, r)
    if r.status_code != 200:
        return "BLOCKED", None, info
    res = parse_jsonld(r.text)
    if res:
        return res
    return "ERROR", None, f"no product JSON-LD found; {info}"


def check_alternate(s):
    return check_jsonld_shop(s, ALTERNATE_URL, "alternate")


def check_expert(s):
    r = s.get(EXPERT_API_URL, headers={"Accept": "application/json",
                                       "Origin": "https://www.expert.de",
                                       "Referer": "https://www.expert.de/"})
    count_bytes(s, len(r.content))
    if r.status_code != 200:
        return "BLOCKED", None, f"HTTP {r.status_code}"
    try:
        p = r.json().get("price") or {}
    except ValueError:
        return "ERROR", None, f"no JSON; {page_info('expert', r)}"
    if not p or "onlineStock" not in p or "onlineButtonAction" not in p:
        return "ERROR", None, "expert response format changed (no onlineStock)"
    online = p.get("onlineStock") or 0
    action = str(p.get("onlineButtonAction") or "NONE")
    price = None
    for k in ("bruttoPrice", "price", "salesPrice", "grossPrice"):
        if isinstance(p.get(k), (int, float, str)):
            price = parse_eur(p.get(k))
            if price:
                break
    in_stock = online > 0 or "CART" in action.upper() or "BUY" in action.upper()
    return ("IN_STOCK" if in_stock else "OUT"), price, f"onlineStock={online} button={action}"


def check_mediamarkt(s):
    return check_mms(s, MEDIAMARKT_URL, "mediamarkt")


def check_saturn(s):
    return check_mms(s, SATURN_URL, "saturn")


# (name, check function, link for the alert, group)
# "free" stores work without the proxy and can be checked more often.
STORES = [
    ("Amazon.de", check_amazon, AMAZON_URL, "proxy"),
    ("PlayStation Store DE", check_psdirect, PSDIRECT_PAGE, "free"),
    ("MediaMarkt.de", check_mediamarkt, MEDIAMARKT_URL, "proxy"),
    ("Saturn.de", check_saturn, SATURN_URL, "proxy"),
    ("Alternate.de", check_alternate, ALTERNATE_URL, "free"),
    ("expert.de", check_expert, EXPERT_URL, "proxy"),
]
GROUPS = ("free", "proxy")


# ------------------------------------------------------------------- main ---
def state_file(group):
    return f"state-{group}.json"


def load_state(group):
    try:
        with open(state_file(group)) as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    return {k: (v if isinstance(v, dict) else {"status": v}) for k, v in state.items()}


def day_stats(state, day):
    return state.setdefault("_stats", {}).setdefault(day, {})


def check_store(fn):
    """Direct first (free); Oxylabs only if the shop blocks GitHub's IP."""
    status, price, note, used = "ERROR", None, "", "direct"
    for use_proxy in (False, True, True):
        try:
            status, price, note = fn(make_session(use_proxy))
        except Exception as e:
            status, price, note = "ERROR", None, str(e)[:160]
            if use_proxy and "CONNECT tunnel failed" in str(e):
                break  # Oxylabs refuses this site; nothing more to try
        used = "proxy" if use_proxy else "direct"
        if status not in ("BLOCKED", "ERROR"):
            break
    return status, price, f"{note} (via {used})"


def handle_result(name, link, status, price, note, st, ts):
    if status == "IN_STOCK" and price and price > MAX_PRICE:
        print(f"[{name}] price {price} above MAX_PRICE, no alert")
        status = "OUT"
    prev = st.get("status")

    if status == "IN_STOCK" and prev != "IN_STOCK":
        alert(name, price, link, st.get("last_restock"))
        log_history(ts, name, "IN_STOCK", price)
        st["last_restock"] = ts
        print(f"[{name}] 🚨 ALERT SENT")
    elif status == "OUT" and prev == "IN_STOCK":
        mins = minutes_between(st.get("last_restock", ts), ts)
        sold_out_alert(name, mins)
        log_history(ts, name, "SOLD_OUT", price, mins)
        st["last_sold_out"] = ts
        print(f"[{name}] sold out again after ~{mins} min")

    ok = status in ("IN_STOCK", "OUT")
    if ok:  # never overwrite the stock status on errors
        st["status"] = status
        if st.get("fail_warned"):
            send_telegram(f"✅ <b>{html.escape(name)}</b>: checking works again.")
        for k in ("fails", "fail_since", "fail_warned"):
            st.pop(k, None)
    else:
        st["fails"] = st.get("fails", 0) + 1
        st.setdefault("fail_since", ts)
        down = minutes_between(st["fail_since"], ts)
        print(f"[{name}] failing for {st['fails']} run(s) / {down} min")
        if not st.get("fail_warned") and st["fails"] >= 3 and down >= FAIL_WARN_MIN:
            send_telegram(
                f"⚠️ <b>{html.escape(name)}</b>: could not be checked for {down} min "
                f"({st['fails']} runs). You may miss a restock there.\n"
                f"Last error: {html.escape(note[:150])}")
            st["fail_warned"] = True
    if st.get("last_restock"):
        print(f"[{name}] last restock: {st['last_restock']}")
    return ok


def restocks_on(day):
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return [r for r in rows if r["time_vilnius"].startswith(day)]


def maybe_send_summary(states, now):
    """Once a day after SUMMARY_HOUR: report on yesterday."""
    meta = states["proxy"].setdefault("_meta", {})
    today = now.strftime("%Y-%m-%d")
    if now.hour < SUMMARY_HOUR or meta.get("last_summary") == today:
        return
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    lines, total_kb, any_stats = [], 0, False
    for name, _, _, group in STORES:
        st = states[group].get("_stats", {}).get(yday, {}).get(name)
        if not st:
            lines.append(f"❔ {html.escape(name)}: no checks")
            continue
        any_stats = True
        total_kb += st.get("proxy_kb", 0)
        icon = "✅" if st["ok"] == st["runs"] else ("⚠️" if st["ok"] else "❌")
        lines.append(f"{icon} {html.escape(name)}: {st['ok']}/{st['runs']} checks OK")
    meta["last_summary"] = today
    if not any_stats:
        return
    events = restocks_on(yday)
    if events:
        ev = "\n" + "\n".join(f"• {e['time_vilnius'][11:]} {html.escape(e['store'])}: "
                       f"{'in stock' if e['event'] == 'IN_STOCK' else 'sold out'}"
                       for e in events)
    else:
        ev = "none"
    send_telegram(
        f"📊 <b>Daily report for {yday}</b>\n" + "\n".join(lines) +
        f"\n\n<b>Restocks:</b> {ev}\n"
        f"<b>Proxy traffic:</b> ~{total_kb / 1024:.1f} MB (estimate; exact figure in Oxylabs dashboard)")
    print("Daily report sent.")


def main():
    if os.environ.get("TEST_ALERT") == "true":
        send_telegram("✅ PS5 checker test: Telegram works. Example alert follows:")
        alert("Amazon.de (TEST)", 899.99, AMAZON_URL, "2026-10-27 09:05")
        sold_out_alert("Amazon.de (TEST)", 12)
        print("Test messages sent.")

    group = STORE_GROUP if STORE_GROUP in ("free", "proxy", "all") else "all"
    run_groups = GROUPS if group == "all" else (group,)
    states = {g: load_state(g) for g in GROUPS}
    now = datetime.now(TZ)
    ts = now.strftime("%Y-%m-%d %H:%M")
    day = ts[:10]
    print(f"--- PS5 Pro stock check (version 16) --- {ts} Vilnius, group: {group}")

    for name, fn, link, g in STORES:
        if g not in run_groups:
            continue
        before = PROXY_BYTES[0]
        status, price, note = check_store(fn)
        print(f"[{name}] {status} price={price} ({note})")
        ok = handle_result(name, link, status, price, note, states[g].setdefault(name, {}), ts)

        stats = day_stats(states[g], day).setdefault(name, {"runs": 0, "ok": 0, "proxy_kb": 0})
        stats["runs"] += 1
        stats["ok"] += int(ok)
        # compressed transfer is roughly 1/4 of the page size, plus connection overhead
        used = PROXY_BYTES[0] - before
        if used or "(via proxy)" in note:
            stats["proxy_kb"] = round(stats["proxy_kb"] + used / 4 / 1024 + 5, 1)

    if "proxy" in run_groups:
        maybe_send_summary(states, now)

    for g in run_groups:
        st = states[g]
        old_days = sorted(st.get("_stats", {}))[:-3]  # keep 3 days of stats
        for d in old_days:
            del st["_stats"][d]
        with open(state_file(g), "w") as f:
            json.dump(st, f)
    print("--- done ---")


if __name__ == "__main__":
    main()
