"""PS5 Pro stock checker: PlayStation Store DE, MediaMarkt.de, Saturn.de,
Alternate.de, expert.de
Requires: pip install curl_cffi
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OXYLABS_USER, OXYLABS_PASS
Optional: MAX_PRICE (default 900), STORE_GROUP (ps | free | mms | proxy | all)
Restock history is appended to restocks.csv (committed by the workflow).
"""
import csv
import html
import base64
import json
import os
import random
import time
import re
import urllib.parse
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from curl_cffi import requests

# ---------------------------------------------------------------- config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OXY_USER = os.environ["OXYLABS_USER"].strip().strip("\"'")
OXY_PASS = os.environ["OXYLABS_PASS"].strip().strip("\"'")

MAX_PRICE = float(os.environ.get("MAX_PRICE", "900"))  # MSRP 899.99

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
EVERY_MIN = {"ps": 1,  # PlayStation runs as a loop, see PS_EVERY_SEC
             "free": int(os.environ.get("FREE_EVERY_MIN", "1")),
             "mms": int(os.environ.get("MMS_EVERY_MIN", "2")),
             "proxy": int(os.environ.get("PROXY_EVERY_MIN", "5"))}
# PlayStation: one run stays alive for PS_LOOP_SEC and checks every PS_EVERY_SEC
PS_EVERY_SEC = max(10, int(os.environ.get("PS_EVERY_SEC", "20")))
PS_LOOP_SEC = int(os.environ.get("PS_LOOP_SEC", "330"))
# Between those full checks, only Sony's store API is asked (small and fast).
# If its buy button differs from the saved status, a full check runs at once.
# 9 Nov 2026 drop: buy button live ~09:57:00–09:58:15, i.e. about 70–90 seconds.
PS_CTA_EVERY_SEC = int(os.environ.get("PS_CTA_EVERY_SEC", "5"))  # 0 = off
# Optional slower interval for single stores: {"store name": minutes}
STORE_EVERY_MIN = {}


def store_interval(name, group):
    return max(STORE_EVERY_MIN.get(name) or 0, EVERY_MIN[group])


def checks_per_slot(group):
    """PlayStation is checked several times per minute."""
    return max(1, 60 // PS_EVERY_SEC) if group == "ps" else 1
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


# A store showing stock again within this many minutes counts as "flapping"
FLAP_MIN = int(os.environ.get("FLAP_MIN", "30"))


def stock_qty(note):
    m = re.search(r"\bstock: (\d+)", note or "")
    return int(m.group(1)) if m else None


def alert(store, price, link, last_restock=None, qty=None, repeat=None, confirmed=False, why=None):
    # PlayStation: the plain product page, exactly as you'd open it yourself. The page
    # is never cached (no-store), and the direct cart/checkout links failed in real
    # drops (9 Nov 2026), so they are not offered any more.
    p = f"€{price:.2f}" if price else "unknown"
    last = f"\n<b>Last restock here:</b> {last_restock}" if last_restock else ""
    q = f"\n<b>Stock:</b> {qty} unit{'s' if qty != 1 else ''}" if qty is not None else ""
    if confirmed:
        # Show MediaMarkt's own answer, e.g. the promised delivery date
        m = re.search(r"cart: ([^✅]+)", confirmed if isinstance(confirmed, str) else "")
        detail = m.group(1).strip(" ;") if m else "delivery confirmed"
        q += f"\n✅ <b>MediaMarkt cart:</b> {html.escape(detail[:160])}"
    if why:
        q += f"\n<b>Signal:</b> {html.escape(why)}"
    if repeat:
        # Flapping: one message instead of five
        buy = f"<a href=\"{html.escape(link)}\">Buy now</a>"
        send_telegram(f"🔁 <b>{html.escape(store)}</b> shows stock again "
                      f"(#{repeat} in {FLAP_MIN} min). Price: {p}{q}\n{buy}")
        return
    buy = steps = f"<a href=\"{html.escape(link)}\">Buy now</a>"
    if "playstation.com" in link:
        buy = f"<a href=\"{html.escape(link)}\">Open PS5 Pro page</a>"
        steps = f"{buy}\n→ In den Warenkorb → Warenkorb → Weiter zur Kasse → Apple Pay"
    send_telegram(
        f"🚨 <b>PS5 Pro IN STOCK!</b> 🚨\n"
        f"<b>Store:</b> {html.escape(store)}\n"
        f"<b>Price:</b> {p}{q}{last}\n" + steps
    )
    # Repeat a short version so the phone buzzes several times
    for i in range(2, ALERT_REPEAT + 1):
        time.sleep(ALERT_GAP_SEC)
        send_telegram(f"🚨 ({i}/{ALERT_REPEAT}) PS5 Pro IN STOCK at "
                      f"<b>{html.escape(store)}</b>! {buy}")


# Messages about state changes (sold out, warnings, recovery) are queued and only
# sent after the new state has been saved; otherwise a failed save would make
# every following run send the same message again.
AFTER_SAVE = []


def send_after_save(text, silent=False):
    AFTER_SAVE.append((text, silent))


def sold_out_alert(store, minutes, silent=False):
    send_after_save(f"❌ <b>{html.escape(store)}</b>: sold out again "
                    f"(was in stock for about {minutes} min)", silent=silent)


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
            inv = offers.get("inventoryLevel")
            if isinstance(inv, dict):
                inv = inv.get("value")
            qty = f", stock: {int(float(inv))}" if isinstance(inv, (int, float, str)) and str(inv).replace(".", "", 1).isdigit() else ""
            return status, price, f"{AVAIL_EN.get(short, short)}{qty}, product verified"
    return None


# Sony's shop data is embedded far down in the product page (~860 KB in): per SKU
# "stockLevelStatus", plus "inventoryOverride" (Sony's manual lock) and
# "highDemand" (drop/queue mode). The simple product data near the top of the
# page only serves as a fallback.
PS_SKU_MARK = "PS5PRO"
PS_OUT_STATUSES = {"OUT_OF_STOCK", "NO_STOCK", "SOLD_OUT", "NOT_AVAILABLE", "UNAVAILABLE", "DISCONTINUED"}


def ps_unescape(t):
    # the data sits inside a JavaScript string, so its quotes are escaped once or twice
    return t.replace('\\\\', '\\').replace('\\"', '"')


def ps_data_done(t):
    """Stop downloading once the PS5 Pro's shop data (or a waiting queue) has arrived."""
    if "queue-it" in t[:20000].lower():
        return True
    i = t.find(PS_SKU_MARK, 200_000)
    while i >= 0:
        j = t.find("inventoryOverride", i)
        if 0 <= j - i < 3000 and len(t) > j + 300:
            return True
        i = t.find(PS_SKU_MARK, i + 1)
    return False


def parse_ps_shop_data(t):
    """Returns dict(status, override, high_demand, price) for the PS5 Pro SKU, or None."""
    u = ps_unescape(t)
    for m in re.finditer(r'"skus":\[\{"id":"([^"]*' + PS_SKU_MARK + r'[^"]*)"', u):
        seg = u[m.start():m.start() + 3000]
        st = re.search(r'"stockLevelStatus":"([A-Z_]+)"', seg)
        if not st:
            continue
        pa = re.search(r'"physicalAttributes":\{([^}]*)\}', seg)
        attrs = pa.group(1) if pa else ""
        ov = re.search(r'"inventoryOverride":"?([^",}]*)', attrs)
        hd = re.search(r'"highDemand":(true|false)', attrs)
        pr = re.search(r'"price":(\d+)', seg)
        return {"sku": m.group(1),
                "status": st.group(1),
                "override": (ov.group(1) if ov else "") or "none",
                "high_demand": (hd.group(1) if hd else "unknown"),
                "price": int(pr.group(1)) / 100 if pr else None}
    return None


def ps_in_stock(status, override):
    """The one rule both the real check and the test use."""
    return status not in PS_OUT_STATUSES and str(override).lower() != "forceoutofstock"


# Sony's store API that decides which button the shop shows ("In den Warenkorb"
# or not). Found in a HAR of an in-stock product (PlayStation Portal, v61).
# It is a separate, uncached request (no-store), not part of the page data.
PS_GQL_URL = "https://web.np.playstation.com/api/graphql/v1/op"
PS_CTA_HASH = "911a43f47dbbf7b2f3d513d6237dedfd679e57af3f34787667a22ecad6a249cd"
PS_GQL_CLIENT = "@sie-private/web-commerce-anywhere"
PS_GQL_VERSION = "3.48.0-2026.10.2-1.0"
# Store API answers that put a buy button on the page. Normal for the locked
# PS5 Pro is UNAVAILABLE (checked 8 Nov 2026).
PS_BUY_CTAS = {"ADD_TO_CART", "PREORDER", "BUY_NOW"}


# Sony's checkout opened directly, as the page's own buy button does (format from
# the HAR, 8 Nov 2026). Needs you to be logged in to PSN in that browser.
PS_CHECKOUT_CLIENT = "2dec2b13-57dc-4966-8294-6df0d627e109"
PS_LAST_SKU = [None]  # full SKU id from the last PlayStation check
PS_TEST_SKU = "PSD001-PSDE01830_39-PORTALWHITQYMODE-E000ISZU7"  # Portal, in stock


def ps_checkout_link(sku, action="buynow"):
    # no hostUrl: with it, the checkout expects the page's embedded flow and asks
    # for a login again (tested 8 Nov 2026); without it, the PSN session is used
    q = urllib.parse.urlencode({"clientId": PS_CHECKOUT_CLIENT,
                                "privacy-policy": "minimal", "locale": "de-DE"})
    return f"https://checkout.playstation.com/{action}/{sku}?{q}"


def cta_buyable(cta_text):
    m = re.match(r"cta=([A-Z_+]+)", cta_text or "")
    return bool(m) and bool(set(m.group(1).split("+")) & PS_BUY_CTAS)


def ps_product_id(sku):
    """'PSD001-PSDE01830_39-PORTALWHITQYMODE-E000ISZU7' -> product id without the SKU suffix."""
    parts = (sku or "").split("-")
    return "-".join(parts[:3]) if len(parts) >= 4 and parts[0].startswith("PSD") else ""


def ps_cta(s, sku):
    """Ask Sony's store API which buy button it would show for this SKU.
    Returns a short text like 'cta=ADD_TO_CART' or 'cta=NONE (reasons: ...)'."""
    pid = ps_product_id(sku)
    if not pid:
        return f"cta=? (unexpected SKU id {sku!r})"
    params = urllib.parse.urlencode({
        "operationName": "productRetrieveForCtasWithPrice",
        "variables": json.dumps({"productId": pid}, separators=(",", ":")),
        "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": PS_CTA_HASH}},
                                 separators=(",", ":"))})
    try:
        r = s.get(f"{PS_GQL_URL}?{params}", timeout=15, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Origin": "https://www.playstation.com", "Referer": "https://www.playstation.com/",
            "Cache-Control": "no-cache", "Pragma": "no-cache",
            "apollographql-client-name": PS_GQL_CLIENT,
            "apollographql-client-version": PS_GQL_VERSION,
            "x-psn-app-ver": f"{PS_GQL_CLIENT}/{PS_GQL_VERSION}",
            "x-psn-store-locale-override": "de-DE",
            "disable_query_whitelist": "false"})
    except Exception as e:
        return f"cta=error ({str(e)[:60]})"
    if r.status_code != 200:
        return f"cta=error (HTTP {r.status_code})"
    try:
        d = r.json()
    except ValueError:
        return "cta=error (no JSON)"
    if d.get("errors"):
        return f"cta=error ({str(d['errors'][0].get('message', ''))[:60]})"
    prod = (d.get("data") or {}).get("productRetrieve")
    if not prod:
        return "cta=not found"
    types, reasons = [], set()
    for c in prod.get("webctas") or []:
        types.append(str(c.get("type") or "?"))
        for x in ((c.get("meta") or {}).get("ineligibilityReasons") or []):
            if x.get("type"):
                reasons.add(str(x["type"]))
    text = "cta=" + ("+".join(types) if types else "NONE")
    if reasons - {"UNKNOWN"}:
        text += f" (reasons: {', '.join(sorted(reasons))})"
    return text


def list_ps_skus(t):
    """All SKUs in Sony's embedded shop data: [(sku, name, status, override, high_demand, price)]."""
    u = ps_unescape(t)
    out, seen = [], set()
    for m in re.finditer(r'"skus":\[\{"id":"([^"]+)"', u):
        sku = m.group(1)
        seg = u[m.start():m.start() + 3000]
        st = re.search(r'"stockLevelStatus":"([A-Z_]+)"', seg)
        if not st or sku in seen:
            continue
        seen.add(sku)
        name = re.findall(r'"name":"([^"]{4,120})"', u[max(0, m.start() - 6000):m.start()])
        name = [re.sub(r"\\u([0-9a-fA-F]{4})", lambda x: chr(int(x.group(1), 16)), n) for n in name]
        pa = re.search(r'"physicalAttributes":\{([^}]*)\}', seg)
        attrs = pa.group(1) if pa else ""
        ov = re.search(r'"inventoryOverride":"?([^",}]*)', attrs)
        hd = re.search(r'"highDemand":(true|false)', attrs)
        pr = re.search(r'"price":(\d+)', seg)
        out.append((sku, (name[0] if name else "?").replace("\\t", "").strip(), st.group(1),
                    (ov.group(1) if ov else "") or "none", hd.group(1) if hd else "unknown",
                    int(pr.group(1)) / 100 if pr else None))
    return out


class PageFromApi:
    """Minimal response object for a page fetched through the Oxylabs Web API."""
    def __init__(self, status_code, text):
        self.status_code, self.text, self.headers = status_code, text, {}


def fetch_from_germany(url):
    """One page through the Oxylabs Web API with a German IP (test only, ~1 request)."""
    key = (os.environ.get("OXY_WEB_API_KEY") or "").strip()
    if not key:
        return PageFromApi(0, ""), "⚠️ secret OXY_WEB_API_KEY missing"
    if not key.lower().startswith("bearer "):
        key = f"Bearer {key}"
    try:
        r = requests.post("https://webapi.oxylabs.io/v1/scrape", timeout=120,
                          json={"url": url, "output": ["html"], "run_js": False, "location": "DE"},
                          headers={"Content-Type": "application/json", "Authorization": key})
    except Exception as e:
        return PageFromApi(0, ""), f"⚠️ Web API error: {str(e)[:100]}"
    if r.status_code not in (200, 201):
        return PageFromApi(r.status_code, ""), f"⚠️ Web API HTTP {r.status_code}: {r.text[:120]}"
    try:
        page = (r.json().get("results") or [{}])[0].get("html") or ""
    except ValueError:
        page = ""
    if isinstance(page, list):
        page = "".join(str(x) for x in page)
    return PageFromApi(200 if page else 0, str(page)), "from Germany via Web API"


def ps_test(url):
    """Manual test: read Sony's shop data from any product page and report it.
    'de:' in front of the address fetches it from a German IP instead of GitHub (USA).
    Text after a space filters the products, e.g. '<url> DUALSENSEGA' (part of the
    product id or name)."""
    url, _, flt = url.strip().partition(" ")
    flt = flt.strip().lower()
    if url.lower().startswith("de:"):
        url = cache_bust(url[3:].strip())
        r, how = fetch_from_germany(url)
    else:
        s = make_session(False)
        r, how = get_ps_page(s, url, lambda t: False)
        how = f"from GitHub (USA); {how}"
    skus = list_ps_skus(r.text) if r.status_code == 200 else []
    total = len(skus)
    if flt:
        skus = [x for x in skus if flt in x[0].lower() or flt in x[1].lower()]
    head = "🧪 <b>PlayStation test</b>" + (f" <i>({how})</i>" if how else "")
    lines = [head]
    if r.status_code != 200:
        lines.append(f"⚠️ Page could not be loaded (HTTP {r.status_code})")
    elif not total:
        lines.append("⚠️ No stock data found on this page")
    else:
        lines.append(f"{total} product(s) on the page" +
                     (f", {len(skus)} matching <code>{html.escape(flt)}</code>" if flt else ""))
    cta_s = make_session(False)  # the store API is always asked from GitHub
    for sku, name, st, ov, hd, price in skus[:40]:
        verdict = "✅ <b>IN STOCK</b>" if ps_in_stock(st, ov) else "❌ <b>OUT OF STOCK</b>"
        p = f" – €{price:.2f}" if price else ""
        cta = ps_cta(cta_s, sku)
        lines.append(f"{verdict}\n{html.escape(name[:60])}{p}\n"
                     f"<i>status={st}, override={ov}, highDemand={hd}</i>\n"
                     f"<i>store API: {html.escape(cta)}</i>\n<code>{html.escape(sku)}</code>\n"
                     f"<a href=\"{html.escape(ps_checkout_link(sku, 'add'))}\">Add to cart</a>")
    if len(skus) > 40:
        lines.append(f"… {len(skus) - 40} more not shown (add a filter after the address)")
    lines.append(html.escape(url))
    print(re.sub(r"<[^>]+>", "", "\n".join(lines)))
    # Telegram allows ~4096 characters per message: send in parts
    part = ""
    for ln in lines:
        if part and len(part) + len(ln) > 3800:
            send_telegram(part)
            part = head + " <i>(continued)</i>"
        part = f"{part}\n{ln}" if part else ln
    send_telegram(part)


def cache_bust(url):
    """Add a unique query parameter so the CDN can't answer from its cache."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_cb={int(time.time() * 1000)}{random.randint(100, 999)}"


def cache_info(r):
    """Short description of the CDN cache headers, to see whether a page was fresh."""
    h = {k.lower(): v for k, v in (getattr(r, "headers", None) or {}).items()}
    parts = []
    if h.get("age"):
        parts.append(f"age={h['age']}s")
    for k in ("x-cache", "x-akamai-cache-status", "cf-cache-status", "x-cache-status"):
        if h.get(k):
            parts.append(f"{k}={h[k][:40]}")
    if h.get("cache-control"):
        parts.append(f"cc={h['cache-control'][:40]}")
    return ", ".join(parts) or "no cache headers"


def get_ps_page(s, url, done):
    """Sony's page directly, bypassing the CDN cache."""
    r = fetch_until(s, cache_bust(url), done, max_bytes=1_800_000,
                    headers={"Referer": "https://www.playstation.com/de-de/",
                             "Cache-Control": "no-cache", "Pragma": "no-cache"})
    return r, cache_info(r)


def check_psdirect(s):
    if not PSDIRECT_URL.startswith("https://"):
        return "ERROR", None, f"bad PSDIRECT_URL: {PSDIRECT_URL!r}"
    r, how = get_ps_page(s, PSDIRECT_URL, ps_data_done)
    info = page_info("playstation", r) + (f", {how}" if how else "")
    if r.status_code != 200:
        return "BLOCKED", None, f"HTTP {r.status_code} (final URL: {r.url})"

    # A drop often sends visitors to a waiting queue instead of the product page
    if "queue-it" in str(r.url).lower() or "queue-it" in r.text[:20000].lower():
        return "IN_STOCK", None, f"Sony waiting queue is active (drop in progress?); {info}"

    d = parse_ps_shop_data(r.text)
    if d:
        sig = f"override={d['override']}, highDemand={d['high_demand']}"
        PS_LAST_SKU[0] = d["sku"]
        cta = ps_cta(s, d["sku"])
        extra = f"; {cta}" + (f"; {how}" if how else "")
        if ps_in_stock(d["status"], d["override"]):
            return "IN_STOCK", d["price"], f"shop data: {d['status']}; {sig}{extra}"
        # The buy button people see comes from the store API, not the page data
        # (Gran Turismo DualSense: page OUT_OF_STOCK, store API PREORDER, button clickable)
        if cta_buyable(cta):
            return "IN_STOCK", d["price"], (f"store API buy button while page says "
                                            f"{d['status']}; {sig}{extra}")
        return "OUT", d["price"], f"shop data: {d['status']}; {sig}{extra}"

    # Fallback: the simple product data near the top of the page
    res = parse_jsonld(r.text)
    if res:
        return res[0], res[1], f"{res[2]}; shop data not found (fallback); {info}"
    return "ERROR", None, f"no stock data on page; {info}"


# --- MediaMarkt / Saturn cart check -------------------------------------------
# The product page can say "in stock" while no warehouse can ship it (phantom
# stock). When the page shows stock, the PS5 Pro is put into a fresh, anonymous,
# throwaway cart (1 unit, never logged in, never checked out) and the cart's
# DELIVERY status is read. MediaMarkt doesn't reserve stock in the cart.
MMS_PRODUCT_ID = "3018022"
MMS_CART_CHECK = os.environ.get("MMS_CART_CHECK", "true") == "true"
MMS_CLIENT_VERSION = "8.532.1"
MMS_ADD_HASH = "660441ec5b7081ed1b9733a50a5fe25441e4ad53562f4cdff421d3e620b9dd30"
MMS_BASKET_HASH = "a87a1f454d2b2f93fc917e7977a4c70c4a6aecc7b81007444720410f2d511ef4"
MMS_SETADDR_HASH = "fc07599b9591fbc06c793c3fb5f0a7a51a05818c9a55b42ee33b7a05770f2200"


def mms_address():
    """Delivery address from the MMS_ADDRESS secret: 'zip|street|housenumber|city'."""
    parts = [p.strip() for p in (os.environ.get("MMS_ADDRESS") or "").split("|")]
    if len(parts) != 4 or not all(parts):
        return None
    z, street, no, city = parts
    return {"zipcode": z, "street": street, "houseNumber": no, "city": city, "country": "DE"}


def real_dispatch(ref):
    """True if a shippingOptionReference comes from real warehouse sourcing.
    Without an address MediaMarkt sends 'e30=' (= '{}'), with real sourcing it
    contains a dispatchDate."""
    if not ref:
        return False
    try:
        raw = base64.b64decode(ref + "=" * (-len(ref) % 4)).decode("utf-8", "ignore")
    except Exception:
        return False
    return "dispatchDate" in raw
MMS_SHOPS = {"mediamarkt": ("https://www.mediamarkt.de", "Media"),
             "saturn": ("https://www.saturn.de", "Saturn")}


def mms_headers(sales_line, operation):
    return {"accept": "*/*", "content-type": "application/json",
            "apollographql-client-name": "pwa-client-pqm",
            "apollographql-client-version": MMS_CLIENT_VERSION,
            "x-cacheable": "false", "x-flow-id": str(uuid.uuid4()),
            "x-mms-country": "DE", "x-mms-language": "de",
            "x-mms-salesline": sales_line, "x-operation": operation}


def mms_pwa(sales_line):
    return {"captureChannel": "DESKTOP", "salesLine": sales_line, "country": "DE",
            "language": "de", "globalLoyaltyProgram": True, "isOneAccountProgramActive": True,
            "shouldInactiveContractsBeHidden": True, "isUsingXccCustomerComponent": True,
            "isCheckoutPhoneCompareActive": True}


def mms_gql_error(d):
    errs = d.get("errors") or []
    if not errs:
        return None
    e = errs[0]
    ext = e.get("extensions") or {}
    return ext.get("code") or e.get("message") or "unknown error"


def mms_cart_check(s, label, product_url):
    """Returns ('YES' | 'NO' | 'UNKNOWN', detail)."""
    base, line = MMS_SHOPS[label]
    api = f"{base}/api/v1/graphql"
    hdr_ref = {"referer": product_url, "origin": base}
    # 1) add 1 unit to the session's (new, empty) cart
    body = {"operationName": "AddProduct",
            "variables": {"items": [{"outletId": None, "productId": MMS_PRODUCT_ID, "quantity": 1,
                                     "serviceIds": None, "warrantyIds": None}]},
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": MMS_ADD_HASH},
                           "pwa": mms_pwa(line)}}
    r = s.post(api, json=body, headers={**mms_headers(line, "AddProduct"), **hdr_ref})
    count_bytes(s, len(r.content))
    if r.status_code != 200 or "captcha" in r.text[:2000].lower():
        return "UNKNOWN", f"add to cart: HTTP {r.status_code}"
    try:
        err = mms_gql_error(r.json())
    except ValueError:
        return "UNKNOWN", "add to cart: no JSON (maybe a bot check)"
    if err and err != "BASKET_ITEM_MAX_QUANTITY":
        return "UNKNOWN", f"add to cart: {err}"
    try:
        basket_id = (r.json().get("data") or {}).get("addProduct", {}).get("id")
    except (ValueError, AttributeError):
        basket_id = None
    cofr = {"isEnabled": True, "baseDomain": base, "channel": "DESKTOP", "isLegacyDataExcluded": False,
            "features": {"delivery": {"isDeliveryStatusByEarliestDateEnabled": True,
                                      "isLocationSourcingEnabled": True,
                                      "isLocationSourcingMarketplaceEnabled": True},
                         "onlineStatus": {"isParallelRequestsEnabled": True,
                                          "isPermanentlyNaIndexEnabled": True}}}
    addr = mms_address()
    if addr and basket_id:
        # 2a) set the delivery address: only then does MediaMarkt source from a real warehouse
        body = {"operationName": "SetAddress",
                "variables": {"isCustomerBehaviorInfluenceActive": False, "basketId": basket_id,
                              "shipping": addr, "cofrConfig": cofr},
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": MMS_SETADDR_HASH},
                               "pwa": mms_pwa(line)}}
        r = s.post(api, json=body, headers={**mms_headers(line, "SetAddress"), **hdr_ref})
        step = "set address"
    else:
        # 2b) no address: read the cart (gives only a generic estimate, see below)
        variables = {"isThankYouPage": False, "shouldUpdateLoyaltyPoints": False,
                     "isCustomerBehaviorInfluenceActive": True, "cofrConfig": cofr}
        ext = {"persistedQuery": {"version": 1, "sha256Hash": MMS_BASKET_HASH}, "pwa": mms_pwa(line)}
        q = urllib.parse.urlencode({"operationName": "GetBasket",
                                    "variables": json.dumps(variables, separators=(",", ":")),
                                    "extensions": json.dumps(ext, separators=(",", ":"))})
        r = s.get(f"{api}?{q}", headers={**mms_headers(line, "GetBasket"), **hdr_ref})
        step = "read cart"
    count_bytes(s, len(r.content))
    if r.status_code != 200:
        return "UNKNOWN", f"{step}: HTTP {r.status_code}"
    try:
        d = r.json()
    except ValueError:
        return "UNKNOWN", f"{step}: no JSON"
    err = mms_gql_error(d)
    if err and not (d.get("data") or {}).get("basket"):
        return "UNKNOWN", f"{step}: {err}"
    return mms_cart_verdict(d, label, with_address=bool(addr and basket_id))


def mms_cart_verdict(d, label, with_address):
    try:
        checkout = d["data"]["basket"]["content"]["checkout"]
        groups = checkout["mmsGroups"]["groups"]
    except (KeyError, TypeError):
        return "UNKNOWN", "cart: unexpected format"
    for g in groups or []:
        for item in g.get("lineItems") or []:
            if str(item.get("productId")) != MMS_PRODUCT_ID:
                continue
            fl = {f.get("method"): f for f in item.get("fulfillments") or []}
            if "DELIVERY" not in fl:
                return "UNKNOWN", f"no delivery option in cart ({', '.join(fl) or 'none'})"
            reason, promise = fl["DELIVERY"].get("unavailableReason"), fl["DELIVERY"].get("promise")
            ref = next((f.get("shippingOptionReference") for f in g.get("fulfillments") or []
                        if f.get("method") == "DELIVERY"), None)
            sourced = real_dispatch(ref)
            print(f"[{label} cart] address={'yes' if with_address else 'no'} sourced={sourced} " + json.dumps(
                {m: {"reason": f.get("unavailableReason"), "promise": f.get("promise")}
                 for m, f in fl.items()}, ensure_ascii=False)[:600])
            if reason:
                return "NO", f"cart: delivery not possible ({reason})"
            if promise and sourced:
                when = (promise.get("earliest") or "")[:10]
                return "YES", f"cart: warehouse found, delivery {when}"
            if not with_address:
                return "UNSURE", "cart without address gives only a generic estimate (MMS_ADDRESS not set)"
            return "UNSURE", "cart gives no real dispatch date"
    return "UNKNOWN", "PS5 Pro not found in cart"


def mms_test(product_id):
    """Manual test: run the full cart chain (add -> address -> warehouse) for any product."""
    global MMS_PRODUCT_ID
    real_id, MMS_PRODUCT_ID = MMS_PRODUCT_ID, product_id
    mode = True
    lines = [f"🧪 <b>Cart test</b> with product {html.escape(product_id)}",
             "Address: " + ("set ✅" if mms_address() else "⚠️ MMS_ADDRESS missing or wrong format")]
    icons = {"YES": "✅ <b>CONFIRMED</b>", "NO": "❌ <b>NOT AVAILABLE</b>",
             "UNSURE": "🟡 <b>NOT CONFIRMED</b>", "UNKNOWN": "⚠️ <b>CHECK FAILED</b>"}
    try:
        for label in ("mediamarkt", "saturn"):
            base = MMS_SHOPS[label][0]
            page_fn = check_mediamarkt if label == "mediamarkt" else check_saturn
            try:
                pst, _, pnote = page_fn(make_session(mode))
                page = ("✅ loads" if pst in ("OUT", "IN_STOCK", "PHANTOM", "UNSURE")
                        else f"❌ {pst}") + f" ({pnote[:80]})"
            except Exception as e:
                page = f"❌ {type(e).__name__}: {str(e)[:100]}"
            try:
                verdict, detail = mms_cart_check(make_session(mode), label,
                                                 f"{base}/de/product/_test-{product_id}.html")
            except Exception as e:
                verdict, detail = "UNKNOWN", f"{type(e).__name__}: {e}"[:200]
            lines.append(f"{label.capitalize()}: {icons.get(verdict, verdict)}\n"
                         f"<i>PS5 Pro page: {html.escape(page)}\ncart: {html.escape(detail)}</i>")
    finally:
        MMS_PRODUCT_ID = real_id
    text = "\n".join(lines)
    print(re.sub(r"<[^>]+>", "", text))
    send_telegram(text)


def check_mms(s, url, label):
    """MediaMarkt and Saturn share the same shop system."""
    r = fetch_until(s, url, mediamarkt_done, max_bytes=300_000)
    info = page_info(label, r)
    if r.status_code != 200:
        return "BLOCKED", None, info
    res = parse_jsonld(r.text)
    if not res:
        return "ERROR", None, f"no product JSON-LD found; {info}"
    status, price, note = res
    if status != "IN_STOCK" or not MMS_CART_CHECK:
        return res
    # The page says "in stock": ask the cart whether it can actually be shipped
    try:
        verdict, detail = mms_cart_check(s, label, url)
    except Exception as e:
        verdict, detail = "UNKNOWN", f"cart check failed: {str(e)[:100]}"
    if verdict == "YES":
        return "IN_STOCK", price, f"{note}; {detail} ✅"
    if verdict == "NO":
        return "PHANTOM", price, f"page says in stock, but {detail}"
    if verdict == "UNSURE":
        return "UNSURE", price, f"page says in stock, but {detail}"
    # Unknown: better a possibly false alert than a missed restock
    return "IN_STOCK", price, f"{note}; cart check unavailable ({detail})"


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
    return ("IN_STOCK" if in_stock else "OUT"), price, f"{'in stock' if in_stock else 'out of stock'} (stock: {online}, button: {action.lower()})"


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
#   ps    PlayStation, loop every PS_EVERY_SEC seconds (cron job every 5 min)
#   free  Alternate, Müller, every minute
#   mms   MediaMarkt, Saturn, every 2 minutes (proxy + cart + address check)
#   proxy expert, every 5 minutes
STORES = [
    ("PlayStation Store DE", check_psdirect, PSDIRECT_PAGE, "ps"),
    ("MediaMarkt.de", check_mediamarkt, MEDIAMARKT_URL, "mms"),
    ("Saturn.de", check_saturn, SATURN_URL, "mms"),
    ("Alternate.de", check_alternate, ALTERNATE_URL, "free"),
    ("Müller.de", check_mueller, MUELLER_URL, "free"),
    ("expert.de", check_expert, EXPERT_URL, "proxy"),
]
GROUPS = ("ps", "free", "mms", "proxy")


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
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ps5-stock-checker"}  # GitHub rejects requests without one


def gist_quota(r):
    """GitHub's API allowance for the token's user (shared by everything using that
    account's tokens), e.g. 'GitHub API: 4870/5000 left, resets 11:02'."""
    h = r.headers
    left, limit, reset = h.get("x-ratelimit-remaining"), h.get("x-ratelimit-limit"), h.get("x-ratelimit-reset")
    if left is None:
        return ""
    when = datetime.fromtimestamp(int(reset), TZ).strftime("%H:%M") if reset else "?"
    return f"GitHub API: {left}/{limit} left, resets {when} Vilnius"


def gist_error(r):
    """GitHub's own explanation, e.g. 'Resource not accessible by personal access token'."""
    try:
        msg = r.json().get("message", "")
    except Exception:
        msg = r.text[:150]
    q = gist_quota(r)
    return f"HTTP {r.status_code}: {msg[:90]}" + (f" ({q})" if q else "")


def load_all_states():
    """Returns ({group: state}, {group: loaded_ok}). Never raises."""
    raw, ok = {}, {}
    if GIST_ID and GIST_TOKEN:
        raw, ok = load_from_gist()
    else:
        print("No storage configured: using local state files")
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


def load_from_gist():
    raw, ok = {}, {}
    if True:
        try:
            r = requests.get(GIST_API, headers=gist_headers(), timeout=15)
            if r.status_code != 200:
                raise Exception(gist_error(r))
            if gist_quota(r):
                print(gist_quota(r))
            files = r.json().get("files", {})
            for g in GROUPS:
                f = files.get(state_file(g))
                if f is None:
                    raw[g], ok[g] = "{}", True      # file not created yet
                elif f.get("truncated"):
                    rr = requests.get(f["raw_url"], headers=gist_headers(), timeout=15)
                    if rr.status_code != 200:
                        raise Exception(gist_error(rr))
                    raw[g], ok[g] = rr.text, True
                else:
                    raw[g], ok[g] = f.get("content") or "{}", True
        except Exception as e:
            print(f"Could not load state from Gist: {str(e)[:220]}")
            for g in GROUPS:
                raw[g], ok[g] = "{}", False
    return raw, ok


def save_states(states, groups):
    """Writes only the given groups' state, so the two groups never overwrite each other.
    Returns True if saved."""
    if GIST_ID and GIST_TOKEN:
        payload = {"files": {state_file(g): {"content": json.dumps(states[g])} for g in groups}}
        try:
            r = requests.patch(GIST_API, headers=gist_headers(), json=payload, timeout=15)
            if r.status_code != 200:
                raise Exception(gist_error(r))
            return True
        except Exception as e:
            print(f"Could not save state to Gist: {str(e)[:220]}")
            return False
    for g in groups:
        with open(state_file(g), "w") as f:
            json.dump(states[g], f)
    return True


def day_stats(state, day):
    return state.setdefault("_stats", {}).setdefault(day, {})


def check_store(fn, modes=(False, True, True)):
    """Direct first (free); Oxylabs only if the shop blocks GitHub's IP.

    Returns status, price, note and how many requests went direct / via proxy.
    """
    status, price, note, used = "ERROR", None, "", "direct"
    n_direct = n_proxy = 0
    for use_proxy in modes:
        if use_proxy is True:
            n_proxy += 1
        else:
            n_direct += 1
        try:
            status, price, note = fn(make_session(use_proxy))
        except Exception as e:
            status, price, note = "ERROR", None, str(e)[:160]
            if use_proxy is True and "CONNECT tunnel failed" in str(e):
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
    if status in ("UNSURE", "PHANTOM"):
        # Page shows stock but the cart can't confirm a real warehouse: not buyable.
        # No message, only the log.
        print(f"[{name}] page shows stock, cart not confirmed ({status}): treated as out of stock")
        status = "OUT"
    sig = re.search(r"override=([^,;)]+), highDemand=([a-z]+)", note or "")
    if sig:
        now_sig = f"override={sig.group(1)}, highDemand={sig.group(2)}"
        before = st.get("ps_signals")
        if before and before != now_sig:
            send_after_save(f"👀 <b>{html.escape(name)}</b>: Sony changed its shop settings for the "
                            f"PS5 Pro\n<b>Before:</b> {html.escape(before)}\n<b>Now:</b> {html.escape(now_sig)}\n"
                            f"This often happens shortly before a drop. "
                            f"<a href=\"{html.escape(link)}\">Open page</a>")
        st["ps_signals"] = now_sig
    cta = re.search(r"cta=([A-Z_+]+(?: \(reasons: [^)]*\))?)", note or "")
    if cta:  # errors are not compared, only real answers
        before = st.get("ps_cta")
        # buy-button changes get 🚨 / ❌ instead, so 👀 is only for other changes
        if (before and before != cta.group(1) and not cta_buyable("cta=" + cta.group(1))
                and not cta_buyable("cta=" + before)):
            send_after_save(f"👀 <b>{html.escape(name)}</b>: Sony's store API changed the PS5 Pro "
                            f"buy button\n<b>Before:</b> {html.escape(before)}\n"
                            f"<b>Now:</b> {html.escape(cta.group(1))}\n"
                            f"<a href=\"{html.escape(link)}\">Open page</a>")
        st["ps_cta"] = cta.group(1)
        if st.pop("cta_fail_warned", None):
            send_after_save(f"✅ <b>{html.escape(name)}</b>: Sony's store API answers again.")
        st.pop("cta_fail_since", None)
    elif re.search(r"cta=(error|\?)", note or ""):
        # The page check keeps working, so the normal ⚠️ warning would never fire
        st.setdefault("cta_fail_since", ts)
        down = minutes_between(st["cta_fail_since"], ts)
        if not st.get("cta_fail_warned") and down >= FAIL_WARN_MIN:
            err = re.search(r"cta=[^;]*", note).group(0)
            send_after_save(f"⚠️ <b>{html.escape(name)}</b>: Sony's store API has failed for {down} min "
                            f"({html.escape(err[:80])}). Only the page data is checked now, "
                            f"so a drop like the 6 Nov ones could be missed.")
            st["cta_fail_warned"] = True
    if status == "IN_STOCK" and price and price > MAX_PRICE:
        print(f"[{name}] price {price} above MAX_PRICE, no alert")
        status = "OUT"
    prev = st.get("status")

    if status == "IN_STOCK" and prev != "IN_STOCK":
        qty = stock_qty(note)
        why = None
        if "store API buy button" in (note or ""):
            c = re.search(r"cta=([A-Z_+]+)", note)
            why = (f"Sony's store API shows a buy button ({c.group(1) if c else '?'}), "
                   f"page data still says out of stock. Can still fail at checkout.")
        last_loud = st.get("last_loud_alert")
        # PlayStation drops are real and short (~1 min, sometimes twice within 30 min),
        # so every one gets the full alert; flapping protection is for the other stores
        flapping = (last_loud and minutes_between(last_loud, ts) < FLAP_MIN
                    and "PlayStation" not in name)
        if flapping:
            st["flaps"] = st.get("flaps", 1) + 1
            alert(name, price, link, qty=qty, repeat=st["flaps"], confirmed=note if "✅" in note else False,
                  why=why)
            print(f"[{name}] 🔁 stock again (flap #{st['flaps']}), single message sent")
        else:
            st["flaps"] = 1
            st["last_loud_alert"] = ts
            alert(name, price, link, st.get("last_restock"), qty=qty, confirmed=note if "✅" in note else False,
                  why=why)
            print(f"[{name}] 🚨 ALERT SENT")
        log_history(ts, name, "IN_STOCK", price)
        st["last_restock"] = ts
    elif status == "OUT" and prev == "IN_STOCK":
        mins = minutes_between(st.get("last_restock", ts), ts)
        last_loud = st.get("last_loud_alert")
        sold_out_alert(name, mins, silent=bool(last_loud and minutes_between(last_loud, ts) < FLAP_MIN))
        log_history(ts, name, "SOLD_OUT", price, mins)
        st["last_sold_out"] = ts
        print(f"[{name}] sold out again after ~{mins} min")

    ok = status in ("IN_STOCK", "OUT")
    if ok:  # never overwrite the stock status on errors
        st["status"] = status
        if st.get("fail_warned"):
            send_after_save(f"✅ <b>{html.escape(name)}</b>: checking works again.")
        for k in ("fails", "fail_since", "fail_warned"):
            st.pop(k, None)
    else:
        st["fails"] = st.get("fails", 0) + 1
        st.setdefault("fail_since", ts)
        down = minutes_between(st["fail_since"], ts)
        print(f"[{name}] failing for {st['fails']} run(s) / {down} min")
        if not st.get("fail_warned") and st["fails"] >= 3 and down >= FAIL_WARN_MIN:
            send_after_save(
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
    """'✅ expert.de: 12/12 successful' plus failed/missing details."""
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


def expected_checks(every, start, end):
    """How many checks a store checked every `every` minutes should have had in [start, end)."""
    n, t = 0, start.replace(second=0, microsecond=0)
    while t < end:
        if CHECK_FROM <= t.hour <= CHECK_TO and t.minute % every == 0:
            n += 1
        t += timedelta(minutes=1)
    return n


def hour_data(states, key):
    """key 'YYYY-MM-DD HH' -> {store: (done, ok, proxy_kb)}"""
    out = {}
    for name, _, _, g in STORES:
        h = states[g].get("_hourly", {}).get(key, {}).get(name)
        if h:
            out[name] = (h["checks"], h["ok"], h["proxy_kb"], h.get("api", 0))
    return out


def day_data(states, day):
    out = {}
    for name, _, _, g in STORES:
        d = states[g].get("_stats", {}).get(day, {}).get(name)
        if d:
            out[name] = (d["runs"], d["ok"], d.get("proxy_kb", 0), d.get("api", 0))
    return out


def build_report(title, data, start, end, restock_day=None):
    if not data:
        return None
    lines, tot_ok, tot_exp, kb, api = [], 0, 0, 0.0, 0
    for name, _, _, g in STORES:
        done, ok, k, a = data.get(name, (0, 0, 0, 0))
        line, ok, exp = report_line(name, done, ok,
                                    expected_checks(store_interval(name, g), start, end) * checks_per_slot(g))
        lines.append(line)
        tot_ok += ok
        tot_exp += exp
        kb += k
        api += a
    text = f"{title}\n" + "\n".join(lines) + f"\n\n<b>Total:</b> {tot_ok}/{tot_exp} successful"
    if restock_day:
        events = restocks_on(restock_day)
        ev = ("\n" + "\n".join(
            f"• {e['time_vilnius'][11:]} {html.escape(e['store'])}: "
            f"{'in stock' if e['event'] == 'IN_STOCK' else 'sold out'}" for e in events)
              ) if events else "none"
        text += f"\n<b>Restocks:</b> {ev}"
    text += f"\n<b>Proxy traffic:</b> ~{kb / 1024:.2f} MB (estimate)"
    return text


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


def run_store(name, fn, link, g, states, now):
    """One check of one store, including alerts and statistics. Returns the status."""
    ts = now.strftime("%Y-%m-%d %H:%M")
    day = ts[:10]
    before = PROXY_BYTES[0]
    api_n = 0
    status, price, note, n_direct, n_proxy = check_store(fn)
    print(f"{now.strftime('%H:%M:%S')} [{name}] {status} price={price} ({note})")
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
    stats["api"] = stats.get("api", 0) + api_n

    h = states[g].setdefault("_hourly", {}).setdefault(ts[:13], {}).setdefault(
        name, {"checks": 0, "ok": 0, "req_direct": 0, "req_proxy": 0, "proxy_kb": 0})
    h["checks"] += 1
    h["ok"] += int(ok)
    h["req_direct"] += n_direct
    h["req_proxy"] += n_proxy
    h["proxy_kb"] = round(h["proxy_kb"] + kb, 1)
    h["api"] = h.get("api", 0) + api_n
    return states[g][name].get("status")


def flush_after_save(states, groups):
    """Save state now and send the queued status messages (used inside the loop)."""
    if save_states(states, groups) and AFTER_SAVE:
        for text, silent in AFTER_SAVE:
            send_telegram(text, silent=silent)
        AFTER_SAVE.clear()


def ps_loop(states, loaded):
    """PlayStation: full check every PS_EVERY_SEC seconds for PS_LOOP_SEC, and a quick
    store-API-only check every PS_CTA_EVERY_SEC seconds in between. Drops last ~1
    minute, so a fresh run per minute (plus GitHub's start delay) is too slow."""
    name, fn, link, g = next(x for x in STORES if x[3] == "ps")
    end = time.monotonic() + PS_LOOP_SEC
    quick = 0 < PS_CTA_EVERY_SEC < PS_EVERY_SEC
    qs = make_session(False) if quick else None
    n = nq = 0
    next_full = time.monotonic()
    while True:
        now = time.monotonic()
        prev = states[g].get(name, {}).get("status")
        full = now >= next_full
        if not full and PS_LAST_SKU[0]:
            nq += 1
            cta = ps_cta(qs, PS_LAST_SKU[0])
            # errors are ignored here; the full checks report them
            if not cta.startswith(("cta=error", "cta=?")) and cta_buyable(cta) != (prev == "IN_STOCK"):
                print(f"{datetime.now(TZ):%H:%M:%S} [{name}] quick store API check: {cta} -> full check now")
                full = True
        if full:
            status = run_store(name, fn, link, g, states, datetime.now(TZ))
            n += 1
            if loaded[g] and (AFTER_SAVE or status != prev):
                flush_after_save(states, [g])  # changes are saved and announced right away
            next_full = time.monotonic() + PS_EVERY_SEC
        step = PS_CTA_EVERY_SEC if quick else PS_EVERY_SEC
        wake = min(next_full, now + step)
        if wake >= end:
            break
        time.sleep(max(0, wake - time.monotonic()))
    print(f"[{name}] {n} full checks, {nq} quick store API checks in this run")


def main():
    if os.environ.get("TEST_ALERT") == "true":
        send_telegram("✅ PS5 checker test: Telegram works. Example alert follows:")
        alert("MediaMarkt.de (TEST)", 899.99, MEDIAMARKT_URL, "2026-10-27 09:05")
        sold_out_alert("MediaMarkt.de (TEST)", 12)
        # PlayStation example with working links: they point at the in-stock
        # PlayStation Portal, so "Add to cart" really adds a Portal (remove it after)
        global ALERT_REPEAT
        reps, ALERT_REPEAT = ALERT_REPEAT, 1
        alert("PlayStation Store DE (TEST)", 899.99, PSDIRECT_PAGE)
        ALERT_REPEAT = reps
        print("Test messages sent.")

    mms_test_product = (os.environ.get("MMS_TEST_PRODUCT") or "").strip()
    if mms_test_product:
        mms_test(mms_test_product)
        return

    ps_test_url = (os.environ.get("PS_TEST_URL") or "").strip()
    if ps_test_url:
        ps_test(ps_test_url)
        return

    report = (os.environ.get("REPORT") or "none").strip().lower()
    if report != "none":
        states, _ = load_all_states()
        text = report_for(report, states, datetime.now(TZ))
        send_telegram(text or f"No check data for '{report}' yet.")
        print(f"Report '{report}' sent (no checks run).")
        print(text or "no data")
        return

    group = STORE_GROUP if STORE_GROUP in GROUPS + ("all",) else "all"
    run_groups = GROUPS if group == "all" else (group,)
    states, loaded = load_all_states()
    # A store that moved to another group keeps its saved status and history
    for name, _, _, g in STORES:
        if name not in states[g]:
            for other in GROUPS:
                if other != g and name in states[other]:
                    states[g][name] = dict(states[other][name])
                    break
    now = datetime.now(TZ)
    ts = now.strftime("%Y-%m-%d %H:%M")
    print(f"--- PS5 Pro stock check (version 63) --- {ts} Vilnius, group: {group}")

    for name, fn, link, g in STORES:
        if g not in run_groups or g == "ps" and group == "ps":
            continue  # the PlayStation loop runs below
        every = store_interval(name, g)
        if every > EVERY_MIN[g] and now.minute % every != 0:
            continue  # this store has a slower interval and isn't due this minute
        run_store(name, fn, link, g, states, now)

    if group == "ps":
        ps_loop(states, loaded)

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
    saved = save_states(states, to_save) if to_save else False
    if saved:
        for text, silent in AFTER_SAVE:
            send_telegram(text, silent=silent)
    elif AFTER_SAVE:
        print(f"{len(AFTER_SAVE)} status message(s) held back: state not saved, "
              "they will be sent once saving works again")
    print("--- done ---")


if __name__ == "__main__":
    main()
