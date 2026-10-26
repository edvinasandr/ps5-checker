"""PS5 Pro stock checker: Amazon.de, PlayStation Direct DE, MediaMarkt.de
Requires: pip install curl_cffi
Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OXYLABS_USER, OXYLABS_PASS
Optional: MAX_PRICE (default 850), STATE_FILE (default state.json)
"""
import html
import json
import os
import random
import re
import urllib.parse

from curl_cffi import requests

# ---------------------------------------------------------------- config ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
OXY_USER = os.environ["OXYLABS_USER"].strip().strip("\"'")
OXY_PASS = os.environ["OXYLABS_PASS"].strip().strip("\"'")

MAX_PRICE = float(os.environ.get("MAX_PRICE", "900"))  # MSRP 899.99
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

AMAZON_URL = "https://www.amazon.de/dp/B0FRYBZR91"
MEDIAMARKT_URL = "https://www.mediamarkt.de/de/product/_sony-playstationr5-pro-2-tb-konsole-3018022.html"

# Sony shop (PS Direct moved to playstation.com/de-de/store/hardware in Oct 2026).
# Set the GitHub variable PSDIRECT_URL to EITHER the PS5 Pro product page in
# Sony's store OR a JSON stock request URL copied from DevTools.
PSDIRECT_URL = (os.environ.get("PSDIRECT_URL")
                or "https://www.playstation.com/de-de/ps5/ps5-pro/buy-now/")
# Link sent in the Telegram alert:
PSDIRECT_PAGE = os.environ.get("PSDIRECT_PAGE") or PSDIRECT_URL


# ---------------------------------------------------------------- helpers ---
def make_session(use_proxy=True):
    """New sticky residential IP per call (different IP per store)."""
    if not use_proxy:
        s = requests.Session(impersonate="chrome", timeout=25)
        s.headers.update({"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
        return s
    sid = random.randint(1_000_000_000, 9_999_999_999)
    pw = urllib.parse.quote(OXY_PASS, safe="")
    proxy = (f"http://customer-{OXY_USER}-cc-de-sessid-{sid}-sesstime-10:"
             f"{pw}@pr.oxylabs.io:7777")
    s = requests.Session(impersonate="chrome", timeout=25,
                         proxies={"http": proxy, "https": proxy})
    s.headers.update({"Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
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
            f.write(r.text)
    m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    title = html.unescape(m.group(1)).strip()[:70] if m else "no title"
    return f"HTTP {r.status_code}, {len(r.text)} bytes, title='{title}'"


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


def alert(store, price, link):
    p = f"€{price:.2f}" if price else "unbekannt"
    send_telegram(
        f"🚨 <b>PS5 Pro AUF LAGER!</b> 🚨\n"
        f"<b>Händler:</b> {html.escape(store)}\n"
        f"<b>Preis:</b> {p}\n"
        f"<a href=\"{html.escape(link)}\">Jetzt kaufen</a>"
    )


# ----------------------------------------------------------------- checks ---
# Each returns (status, price, note); status: IN_STOCK | OUT | BLOCKED | ERROR

def check_amazon(s):
    r = s.get(AMAZON_URL)
    t = r.text
    info = page_info("amazon", r)
    if r.status_code != 200 or "captcha" in t.lower():
        return "BLOCKED", None, f"captcha/blocked; {info}"

    has_cart = 'id="add-to-cart-button"' in t

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
            status = "IN_STOCK" if avail.endswith("InStock") else "OUT"
            name = str(item.get("name", "")).strip()[:40]
            return status, price, f"{avail.rsplit('/', 1)[-1]} [{name}]"
    return None


def check_psdirect(s):
    if not PSDIRECT_URL.startswith("https://"):
        return "ERROR", None, "PSDIRECT_URL not set"
    r = s.get(PSDIRECT_URL, headers={"Referer": "https://www.playstation.com/de-de/"})
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


def check_mediamarkt(s):
    r = s.get(MEDIAMARKT_URL)
    info = page_info("mediamarkt", r)
    if r.status_code != 200:
        return "BLOCKED", None, info
    res = parse_jsonld(r.text)
    if res:
        return res
    return "ERROR", None, f"no product JSON-LD found; {info}"


STORES = [
    ("Amazon.de", check_amazon, AMAZON_URL),
    ("PlayStation Store DE", check_psdirect, PSDIRECT_PAGE),
    ("MediaMarkt.de", check_mediamarkt, MEDIAMARKT_URL),
]


# ------------------------------------------------------------------- main ---
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main():
    if os.environ.get("TEST_ALERT") == "true":
        send_telegram("✅ PS5 checker test: Telegram works.")
        print("Test message sent.")
    state = load_state()
    print("--- PS5 Pro stock check ---")
    for name, fn, link in STORES:
        status, price, note = "ERROR", None, ""
        use_proxy = True
        for attempt in range(3):  # retry with a fresh IP if blocked
            try:
                status, price, note = fn(make_session(use_proxy))
            except Exception as e:
                status, price, note = "ERROR", None, str(e)[:160]
                if use_proxy and "CONNECT tunnel failed" in str(e):
                    print(f"[{name}] proxy refused this site, trying without proxy")
                    use_proxy = False
                    continue
            if status not in ("BLOCKED", "ERROR"):
                break
        if not use_proxy:
            note += " (no proxy)"

        print(f"[{name}] {status} price={price} ({note})")

        if status == "IN_STOCK" and price and price > MAX_PRICE:
            print(f"[{name}] price {price} above MAX_PRICE, no alert")
            status = "OUT"

        if status == "IN_STOCK" and state.get(name) != "IN_STOCK":
            alert(name, price, link)
            print(f"[{name}] 🚨 ALERT SENT")

        if status in ("IN_STOCK", "OUT"):  # don't overwrite state on errors
            state[name] = status

    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print("--- done ---")


if __name__ == "__main__":
    main()
