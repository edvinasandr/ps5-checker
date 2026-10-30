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
import time
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
MUELLER_URL = "https://www.mueller.de/p/playstation5-pro-konsole-2-tb-IPN3068364/"

# expert loads stock via this API (store chosen by the website for you):
EXPERT_API_URL = "https://production.brntgs.expert.de/api/pricepds?webcode=11364201744&storeId=e_2879130"

TZ = ZoneInfo("Europe/Vilnius")
# Warn on Telegram when a store has failed for this long (and at least 3 runs)
FAIL_WARN_MIN = int(os.environ.get("FAIL_WARN_MIN", "30"))
HISTORY_FILE = "restocks.csv"
# Which stores this run checks: "free" (no proxy), "proxy" or "all"
STORE_GROUP = (os.environ.get("STORE_GROUP") or "all").strip().lower()
SUMMARY_HOUR = 8  # daily report is sent by the first proxy/all run after 08:00
# Your cron-job.org schedule, used to work out how many checks *should* happen
EVERY_MIN = {"free": int(os.environ.get("FREE_EVERY_MIN", "2")),
             "proxy": int(os.environ.get("PROXY_EVERY_MIN", "5"))}
CHECK_FROM = int(os.environ.get("CHECK_FROM", "8"))   # first hour with checks
CHECK_TO = int(os.environ.get("CHECK_TO", "22"))      # last hour with checks
# Hourly reports are only sent between these hours (Vilnius time, inclusive)
HOURLY_FROM = int(os.environ.get("HOURLY_FROM", "7"))
HOURLY_TO = int(os.environ.get("HOURLY_TO", "22"))

# schema.org availability values that mean "you can order it now"
BUYABLE = {"InStock", "LimitedAvailability", "OnlineOnly", "PreOrder", "PreSale", "BackOrder"}
AVAIL_EN = {"InStock": "in stock", "OutOfStock": "out of stock", "LimitedAvailability": "limited stock",
            "PreOrder": "pre-order", "PreSale": "pre-sale", "BackOrder": "backorder",
            "OnlineOnly": "in stock (online only)", "InStoreOnly": "in store only",
            "SoldOut": "sold out", "Discontinued": "discontinued"}
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
    return f"HTTP {r.status_code}, {len(r.text) // 1024} KB"


def page_title(r):
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    return html.unescape(m.group(1)).strip()[:60] if m else "no title"


class Partial:
    """Minimal response object for a partially downloaded page."""
    def __init__(self, status_code, text, headers, url):
        self.status_code, self.text, self.headers, self.url = status_code, text, headers, url


def fetch_until(s, url, done, max_bytes, headers=None):
    """Download only until done(text) is True or max_bytes is reached.

    Saves proxy traffic: the stock info sits near the top of the page.
    """
    r = s.get(url, stream=True, headers=headers)
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


def send_telegram(text, silent=False):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True if silent else False,
                  "disable_notification": silent},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram error: {e}")


# How many times the in-stock alert is sent, and the pause between messages
ALERT_REPEAT = int(os.environ.get("ALERT_REPEAT", "5"))
ALERT_GAP_SEC = float(os.environ.get("ALERT_GAP_SEC", "3"))


def alert(store, price, link, last_restock=None):
    p = f"€{price:.2f}" if price else "unknown"
    last = f"\n<b>Last restock here:</b> {last_restock}" if last_restock else ""
    buy = (f"<a href=\"{html.escape(link)}\">Buy now</a>"
           + "".join(f"  |  <a href=\"{html.escape(u)}\">🛒 Add to cart</a>"
                     for k, u in CART_LINKS.items() if store.startswith(k)))
    send_telegram(
        f"🚨 <b>PS5 Pro IN STOCK!</b> 🚨\n"
        f"<b>Store:</b> {html.escape(store)}\n"
        f"<b>Price:</b> {p}{last}\n" + buy
    )
    # Repeat a short version so the phone buzzes several times
    for i in range(2, ALERT_REPEAT + 1):
        time.sleep(ALERT_GAP_SEC)
        send_telegram(f"🚨 ({i}/{ALERT_REPEAT}) PS5 Pro IN STOCK at "
                      f"<b>{html.escape(store)}</b>! {buy}")


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
        return "ERROR", None, f"not the PS5 Pro page (title: '{page_title(r)}'); {info}"
    if 'id="buybox"' not in t and 'id="desktop_buybox"' not in t:
        return "ERROR", None, f"page layout changed (no buy box area); {info}"
    if not amazon_done(t):
        return "ERROR", None, f"page incomplete or layout changed; {info}"

    # Where Amazon thinks we are: GitHub's servers are mostly in the US, and a
    # non-German visitor may see "cannot be shipped" instead of the buy box.
    loc = ""
    i = t.find('id="glow-ingress-line1"')
    if i >= 0:
        loc = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t[i:i + 400])))
        loc = loc.split(">", 1)[-1].split("Standort")[0].strip()
    german = bool(re.match(r"Lieferung an .+\b\d{5}\b", loc))
    loc_en = re.sub(r"^Lieferung (an|nach)\s*", "", loc)[:35] or "unknown"
    if not german and not getattr(s, "via_proxy", False):
        return "BLOCKED", None, f"direct page not for Germany (delivery to: {loc_en})"

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
        return "IN_STOCK", price, f"buy button, sold by Amazon; delivery to {loc_en}"
    if has_cart:
        why = "buy button, but seller is not Amazon"
    else:
        why = "no buy button"
    return "OUT", price, f"{why}; delivery to {loc_en}; {info}"


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
                return "ERROR", price, f"wrong product on page: '{name}'"
            status = "IN_STOCK" if short in BUYABLE else "OUT"
            return status, price, f"{AVAIL_EN.get(short, short)}, product verified"
    return None


def check_psdirect(s):
    if not PSDIRECT_URL.startswith("https://"):
        return "ERROR", None, f"bad PSDIRECT_URL: {PSDIRECT_URL!r}"
    # The product data ends ~34 KB into a ~950 KB page: stop there (faster, fewer timeouts)
    r = fetch_until(s, PSDIRECT_URL, jsonld_offer_done, max_bytes=1_500_000,
                    headers={"Referer": "https://www.playstation.com/de-de/"})
    info = page_info("playstation", r)
    if r.status_code != 200:
        return "BLOCKED", None, f"HTTP {r.status_code} (final URL: {r.url})"

    # Case 1: JSON stock request copied from DevTools
    if "json" in r.headers.get("content-type", ""):
        data = json.loads(r.text)
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
    return ("IN_STOCK" if in_stock else "OUT"), price, f"{'in stock' if in_stock else 'out of stock'} (online stock: {online}, button: {action.lower()})"


def mueller_done(t):
    return jsonld_offer_done(t) or "nicht lieferbar" in t


def check_mueller(s):
    r = fetch_until(s, MUELLER_URL, mueller_done, max_bytes=1_500_000)
    info = page_info("mueller", r)
    if r.status_code != 200:
        return "BLOCKED", None, info
    t = r.text
    title = page_title(r).replace("®", "")
    if not EXPECTED_NAME.search(title):
        return "BLOCKED", None, f"not the product page, maybe a security check (title: '{title[:40]}'); {info}"
    # 1) standard product data, if Müller provides it
    res = parse_jsonld(t)
    if res:
        return res[0], res[1], f"{res[2]}; {info}"
    # 2) otherwise the availability text shown on the page
    m = re.search(r"(\d{1,4}(?:\.\d{3})*,\d{2})\s*(?:&nbsp;|\s)*€", t)
    price = parse_eur(m.group(1)) if m else None
    if re.search(r"(?i)nicht\s+lieferbar|ausverkauft|nicht\s+verf(?:ü|&uuml;)gbar", t):
        return "OUT", price, f"out of stock (page text), product verified; {info}"
    if "In den Warenkorb" in t:
        return "IN_STOCK", price, f"'add to cart' button found, product verified; {info}"
    return "ERROR", price, f"stock status not found on page; {info}"


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
    ("Müller.de", check_mueller, MUELLER_URL, "free"),
]
GROUPS = ("free", "proxy")


# ------------------------------------------------------------------- main ---
# Saved state lives in a private GitHub Gist (one file per group), read and
# written through the GitHub API. Falls back to local files if not configured.
GIST_ID = (os.environ.get("GIST_ID") or "").strip()
GIST_TOKEN = (os.environ.get("GIST_TOKEN") or "").strip()
GIST_API = f"https://api.github.com/gists/{GIST_ID}"


def state_file(group):
    return f"state-{group}.json"


def gist_headers():
    return {"Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def load_all_states():
    """Returns ({group: state}, {group: loaded_ok}). Never raises."""
    raw, ok = {}, {}
    if GIST_ID and GIST_TOKEN:
        try:
            r = requests.get(GIST_API, headers=gist_headers(), timeout=15)
            r.raise_for_status()
            files = r.json().get("files", {})
            for g in GROUPS:
                f = files.get(state_file(g))
                if f is None:
                    raw[g], ok[g] = "{}", True      # file not created yet
                elif f.get("truncated"):
                    rr = requests.get(f["raw_url"], timeout=15)
                    rr.raise_for_status()
                    raw[g], ok[g] = rr.text, True
                else:
                    raw[g], ok[g] = f.get("content") or "{}", True
        except Exception as e:
            print(f"Could not load state from Gist: {str(e)[:150]}")
            for g in GROUPS:
                raw[g], ok[g] = "{}", False
    else:
        print("GIST_ID / GIST_TOKEN not set: using local state files")
        for g in GROUPS:
            try:
                with open(state_file(g)) as f:
                    raw[g] = f.read()
            except FileNotFoundError:
                raw[g] = "{}"
            ok[g] = True
    states = {}
    for g in GROUPS:
        try:
            st = json.loads(raw[g] or "{}")
        except json.JSONDecodeError:
            st = {}
        states[g] = {k: (v if isinstance(v, dict) else {"status": v}) for k, v in st.items()}
    return states, ok


def save_states(states, groups):
    """Writes only the given groups' files, so the two groups never overwrite each other."""
    if GIST_ID and GIST_TOKEN:
        payload = {"files": {state_file(g): {"content": json.dumps(states[g])} for g in groups}}
        try:
            r = requests.patch(GIST_API, headers=gist_headers(), json=payload, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print(f"Could not save state to Gist: {str(e)[:150]}")
    else:
        for g in groups:
            with open(state_file(g), "w") as f:
                json.dump(states[g], f)


def day_stats(state, day):
    return state.setdefault("_stats", {}).setdefault(day, {})


def check_store(fn):
    """Direct first (free); Oxylabs only if the shop blocks GitHub's IP.

    Returns status, price, note and how many requests went direct / via proxy.
    """
    status, price, note, used = "ERROR", None, "", "direct"
    n_direct = n_proxy = 0
    for use_proxy in (False, True, True):
        if use_proxy:
            n_proxy += 1
        else:
            n_direct += 1
        try:
            status, price, note = fn(make_session(use_proxy))
        except Exception as e:
            status, price, note = "ERROR", None, str(e)[:160]
            if use_proxy and "CONNECT tunnel failed" in str(e):
                # Oxylabs refuses this site (PlayStation): give direct one more try
                time.sleep(3)
                n_direct += 1
                try:
                    status, price, note = fn(make_session(False))
                except Exception as e2:
                    status, price, note = "ERROR", None, str(e2)[:160]
                used = "direct, 2nd try"
                break
        used = "proxy" if use_proxy else "direct"
        if status not in ("BLOCKED", "ERROR"):
            break
    return status, price, f"{note} (via {used})", n_direct, n_proxy


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


def report_line(name, done, ok, expected):
    """'✅ Amazon.de: 12/12 successful' plus failed/missing details."""
    expected = max(expected, done)
    failed, missing = done - ok, expected - done
    icon = "✅" if ok == expected else ("⚠️" if ok else "❌")
    extra = []
    if failed:
        extra.append(f"{failed} failed")
    if missing:
        extra.append(f"{missing} missing")
    tail = f" ({', '.join(extra)})" if extra else ""
    return f"{icon} {html.escape(name)}: {ok}/{expected} successful{tail}", ok, expected


def expected_checks(group, start, end):
    """How many checks the cron-job.org schedule should have made in [start, end)."""
    n, t = 0, start.replace(second=0, microsecond=0)
    while t < end:
        if CHECK_FROM <= t.hour <= CHECK_TO and t.minute % EVERY_MIN[group] == 0:
            n += 1
        t += timedelta(minutes=1)
    return n


def hour_data(states, key):
    """key 'YYYY-MM-DD HH' -> {store: (done, ok, proxy_kb)}"""
    out = {}
    for name, _, _, g in STORES:
        h = states[g].get("_hourly", {}).get(key, {}).get(name)
        if h:
            out[name] = (h["checks"], h["ok"], h["proxy_kb"])
    return out


def day_data(states, day):
    out = {}
    for name, _, _, g in STORES:
        d = states[g].get("_stats", {}).get(day, {}).get(name)
        if d:
            out[name] = (d["runs"], d["ok"], d.get("proxy_kb", 0))
    return out


def build_report(title, data, start, end, restock_day=None):
    if not data:
        return None
    lines, tot_ok, tot_exp, kb = [], 0, 0, 0.0
    for name, _, _, g in STORES:
        done, ok, k = data.get(name, (0, 0, 0))
        line, ok, exp = report_line(name, done, ok, expected_checks(g, start, end))
        lines.append(line)
        tot_ok += ok
        tot_exp += exp
        kb += k
    text = f"{title}\n" + "\n".join(lines) + f"\n\n<b>Total:</b> {tot_ok}/{tot_exp} successful"
    if restock_day:
        events = restocks_on(restock_day)
        ev = ("\n" + "\n".join(
            f"• {e['time_vilnius'][11:]} {html.escape(e['store'])}: "
            f"{'in stock' if e['event'] == 'IN_STOCK' else 'sold out'}" for e in events)
              ) if events else "none"
        text += f"\n<b>Restocks:</b> {ev}"
    return text + f"\n<b>Proxy traffic:</b> ~{kb / 1024:.2f} MB (estimate)"


def hour_start(dt):
    return dt.replace(minute=0, second=0, microsecond=0)


def day_start(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def report_for(kind, states, now):
    """kind: this hour | last hour | today | yesterday"""
    grace = now - timedelta(minutes=2)  # a run started in the last 2 min may not be saved yet
    if kind == "this hour":
        st = hour_start(now)
        return build_report(f"🕐 <b>This hour so far ({st:%H}:00–{now:%H:%M})</b>",
                            hour_data(states, f"{st:%Y-%m-%d %H}"), st, grace)
    if kind == "last hour":
        st = hour_start(now) - timedelta(hours=1)
        return build_report(f"🕐 <b>Hourly report {st:%H}:00–{st:%H}:59</b>",
                            hour_data(states, f"{st:%Y-%m-%d %H}"), st, st + timedelta(hours=1))
    if kind == "today":
        st = day_start(now)
        return build_report(f"📊 <b>Today so far ({now:%Y-%m-%d}, until {now:%H:%M})</b>",
                            day_data(states, f"{st:%Y-%m-%d}"), st, grace, f"{st:%Y-%m-%d}")
    if kind == "yesterday":
        st = day_start(now) - timedelta(days=1)
        return build_report(f"📊 <b>Daily report for {st:%Y-%m-%d}</b>",
                            day_data(states, f"{st:%Y-%m-%d}"), st, st + timedelta(days=1),
                            f"{st:%Y-%m-%d}")
    return None


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
    meta["last_summary"] = today
    text = report_for("yesterday", states, now)
    if text:
        send_telegram(text, silent=True)
        print("Daily report sent.")


def maybe_send_hourly(states, now):
    """Report on the previous hour, sent by the first proxy run after :05
    (so the free group, which saves in parallel, has finished that hour)."""
    meta = states["proxy"].setdefault("_meta", {})
    this_hour = now.strftime("%Y-%m-%d %H")
    if meta.get("last_hourly") == this_hour or now.minute < 5:
        return
    meta["last_hourly"] = this_hour
    prev = now - timedelta(hours=1)
    if not HOURLY_FROM <= now.hour <= HOURLY_TO or not CHECK_FROM <= prev.hour <= CHECK_TO:
        return
    text = report_for("last hour", states, now)
    if text:
        send_telegram(text, silent=True)
        print("Hourly report sent.")


def main():
    if os.environ.get("TEST_ALERT") == "true":
        send_telegram("✅ PS5 checker test: Telegram works. Example alert follows:")
        alert("Amazon.de (TEST)", 899.99, AMAZON_URL, "2026-10-27 09:05")
        sold_out_alert("Amazon.de (TEST)", 12)
        print("Test messages sent.")

    report = (os.environ.get("REPORT") or "none").strip().lower()
    if report != "none":
        states, _ = load_all_states()
        text = report_for(report, states, datetime.now(TZ))
        send_telegram(text or f"No check data for '{report}' yet.")
        print(f"Report '{report}' sent (no checks run).")
        print(text or "no data")
        return

    group = STORE_GROUP if STORE_GROUP in ("free", "proxy", "all") else "all"
    run_groups = GROUPS if group == "all" else (group,)
    states, loaded = load_all_states()
    now = datetime.now(TZ)
    ts = now.strftime("%Y-%m-%d %H:%M")
    day = ts[:10]
    print(f"--- PS5 Pro stock check (version 27) --- {ts} Vilnius, group: {group}")

    for name, fn, link, g in STORES:
        if g not in run_groups:
            continue
        before = PROXY_BYTES[0]
        status, price, note, n_direct, n_proxy = check_store(fn)
        print(f"[{name}] {status} price={price} ({note})")
        ok = handle_result(name, link, status, price, note, states[g].setdefault(name, {}), ts)

        # compressed transfer is roughly 1/4 of the page size, plus ~5 KB
        # connection overhead per proxy request
        kb = (PROXY_BYTES[0] - before) / 4 / 1024 + 5 * n_proxy

        stats = day_stats(states[g], day).setdefault(name, {"runs": 0, "ok": 0, "proxy_kb": 0})
        stats["runs"] += 1
        stats["ok"] += int(ok)
        stats["proxy_kb"] = round(stats["proxy_kb"] + kb, 1)
        stats["requests"] = stats.get("requests", 0) + n_direct + n_proxy
        stats["proxy_requests"] = stats.get("proxy_requests", 0) + n_proxy

        h = states[g].setdefault("_hourly", {}).setdefault(ts[:13], {}).setdefault(
            name, {"checks": 0, "ok": 0, "req_direct": 0, "req_proxy": 0, "proxy_kb": 0})
        h["checks"] += 1
        h["ok"] += int(ok)
        h["req_direct"] += n_direct
        h["req_proxy"] += n_proxy
        h["proxy_kb"] = round(h["proxy_kb"] + kb, 1)

    if "proxy" in run_groups and loaded["proxy"]:
        maybe_send_summary(states, now)
        maybe_send_hourly(states, now)

    to_save = []
    for g in run_groups:
        if not loaded[g]:
            # Saving would overwrite the good state with an empty one
            print(f"State for group '{g}' was not loaded, so it is not saved this run")
            continue
        st = states[g]
        for d in sorted(st.get("_stats", {}))[:-3]:  # keep 3 days of stats
            del st["_stats"][d]
        for hkey in sorted(st.get("_hourly", {}))[:-48]:  # keep 48 hours
            del st["_hourly"][hkey]
        to_save.append(g)
    if to_save:
        save_states(states, to_save)
    print("--- done ---")


if __name__ == "__main__":
    main()
