#!/opt/homebrew/bin/python3
"""amazon-cli — search Amazon and check prices from the terminal.

Scrapes amazon.com directly. No API key, no signup, no credit card. Uses the
Python standard library, plus optional curl_cffi for browser impersonation.

Not affiliated with, authorized, or endorsed by Amazon.com, Inc. "Amazon" is a
trademark of Amazon.com, Inc. For personal, educational, low-volume use only;
you are responsible for complying with Amazon's Terms of Service.

For each metric the top two are highlighted, best bold and 2nd regular (rest
white): price green, rating cyan, review count blue, delivery yellow.

Results are Prime-eligible, popular brands with 1000+ reviews by default.

Runs from your own machine's IP, so occasional requests normally go through.
Responses are cached for 10 minutes, so repeated searches make no requests.

Usage:
    amazon-cli "4TB 2.5 SATA Internal SSD"             # top 10, best rated (default)
    amazon-cli "4TB 2.5 SATA Internal SSD" --price     # sorted by price
    amazon-cli "4TB 2.5 SATA Internal SSD" --delivery  # sorted by delivery
    amazon-cli -h                                      # all options

Optional:
    AMAZON_TLD=co.uk amazon-cli "ssd"                  # default amazon.com
"""

import argparse
import concurrent.futures
import gzip
import hashlib
import html
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TLD = os.environ.get("AMAZON_TLD", "com")
BASE = f"https://www.amazon.{TLD}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

_TTY = sys.stdout.isatty()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


# Optional browser-impersonation backend: curl_cffi mimics a real Chrome at the
# TLS/HTTP2 fingerprint level, which anti-bot systems fingerprint. Falls back to
# urllib if it isn't installed, so the tool still runs dependency-free.
try:
    from curl_cffi import requests as _cffi
except ImportError:
    _cffi = None

CACHE_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "amazon-cli-cache")
CACHE_TTL = 600  # seconds; identical fetches within this window skip the network


def _cache_file(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest())


def _cache_read(url: str):
    try:
        path = _cache_file(url)
        if time.time() - os.path.getmtime(path) < CACHE_TTL:
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read()
    except OSError:
        pass
    return None


def _cache_write(url: str, text: str) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_cache_file(url), "w", encoding="utf-8", errors="replace") as f:
            f.write(text)
    except OSError:
        pass


def _download(url: str) -> str:
    """Raw GET with gzip handling. Uses curl_cffi (browser impersonation) when
    available, else urllib. Raises on error."""
    if _cffi is not None:
        resp = _cffi.get(url, impersonate="chrome", timeout=30,
                         headers={"Accept-Language": "en-US,en;q=0.9"})
        return resp.text
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _is_throttled(text: str) -> bool:
    """Amazon's bot-check page: a few KB, empty title, no products."""
    low = text.lower()
    return ("captcha" in low or "api-services-support" in low
            or (len(text) < 8000 and "data-asin=" not in text))


def fetch(url: str) -> str:
    """GET a page (served from cache when fresh); exit on a bot-check/throttle."""
    text = _cache_read(url)
    if text is not None:
        return text
    try:
        text = _download(url)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from Amazon.")
    except urllib.error.URLError as e:
        sys.exit(f"Network error reaching Amazon: {e.reason}")
    except Exception as e:
        sys.exit(f"Error fetching Amazon: {e}")
    if _is_throttled(text):
        sys.exit(
            "Amazon is rate-limiting this IP (bot check). Wait a few minutes and\n"
            "retry — identical searches within 10 min are served from cache (no\n"
            "requests), so re-runs are free. Space fresh searches out."
        )
    _cache_write(url, text)
    return text


def _soft_fetch(url: str):
    """Cached, best-effort GET; returns None on error instead of exiting — for
    enrichment where one failure shouldn't kill the run."""
    text = _cache_read(url)
    if text is not None:
        return text
    try:
        text = _download(url)
    except Exception:
        return None
    if not _is_throttled(text):  # don't cache bot-check pages
        _cache_write(url, text)
    return text


def _model(text: str):
    """Extract the manufacturer model number from a product page's details
    table (labelled 'Model Number' or 'Item model number'). Only real SKUs are
    returned — a single token with letters and digits; generic values like
    'Platinum 4TB' (multi-word) are rejected."""
    m = re.search(r'(?:Item model number|Model Number)\s*</th>\s*'
                  r'<td[^>]*>\s*([^<]{1,40}?)\s*</td>', text, re.I)
    if not m:
        return None
    val = _clean(m.group(1))
    if " " in val or not (any(ch.isdigit() for ch in val)
                          and any(ch.isalpha() for ch in val)):
        return None
    return val


def _product_details(url: str):
    """Fetch a product page; return (review_count, delivery, model)."""
    page = _soft_fetch(url)
    if not page:
        return None, None, None
    # The total lives in #acrCustomerReviewText, rendered as "(36,813)". A bare
    # "N ratings" match is unsafe — it grabs related-product counts elsewhere.
    m = re.search(r'acrCustomerReviewText[^>]*>\s*\(?\s*([\d,]+)', page)
    count = int(m.group(1).replace(",", "")) if m else None
    return count, _delivery(page), _model(page)


def _enrich_counts(items: list[dict]) -> None:
    """Fetch each product page in parallel for its authoritative review count,
    model number, and (when the search page lacked one) delivery date. Mutates
    in place; keeps the search-page value when a fetch comes up empty."""
    if not items:
        return
    # Gentle concurrency: a slow trickle of product-page fetches looks far less
    # bot-like than a burst, which reduces rate-limit blocks (impersonation only
    # defeats fingerprinting, not volume).
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        details = list(pool.map(lambda x: _product_details(x["url"]), items))
    for r, (count, deliv, model) in zip(items, details):
        if count is not None:
            r["ratings_total"] = count
        if not r.get("delivery") and deliv:
            r["delivery"] = deliv
        if model:
            r["model"] = model


def _delivery(text: str):
    """Extract the SOONEST delivery date shown for a product. Pages list a
    standard 'FREE delivery <date>' and an 'Or fastest delivery <date>' — we
    want the fastest. Dates sit in bold span/b/strong tags on search & product
    pages."""
    tag = r'<(?:span|b|strong)[^>]*>'
    cands = [_clean(d) for d in
             re.findall(r'delivery[^<]{0,15}' + tag + r'([A-Za-z0-9][^<]{2,40})</',
                        text, re.I)]
    cands = [d for d in cands if d]
    if not cands:
        return None
    # Pick the soonest (Today < Tomorrow < dated < unknown).
    return min(cands, key=lambda d: _delivery_key({"delivery": d}))


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _delivery_key(item: dict):
    """Sort key for soonest delivery: Today < Tomorrow < dated < unknown."""
    d = (item.get("delivery") or "").lower()
    if not d:
        return (9, 99, 99)
    if "today" in d:
        return (0, 0, 0)
    if "tomorrow" in d:
        return (1, 0, 0)
    m = re.search(r'([a-z]{3})[a-z]*\s+(\d{1,2})', d)
    if m:
        return (2, _MONTHS.get(m.group(1), 12), int(m.group(2)))
    return (9, 99, 99)


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", s)).strip()


def parse_search(page: str) -> list[dict]:
    """Extract product rows from an Amazon search results page."""
    items: list[dict] = []
    seen: set[str] = set()
    # Each result is anchored by data-component-type="s-search-result".
    marker = 's-search-result'
    positions = [m.start() for m in re.finditer(re.escape(marker), page)]
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(page)
        # ASIN lives on the enclosing div, just before the marker.
        head = page[max(0, pos - 400):pos]
        asin_m = re.findall(r'data-asin="([A-Z0-9]{10})"', head)
        block = page[pos:end]

        title_m = re.search(r'<h2[^>]*>.*?<span[^>]*>([^<]+)</span>', block, re.S)
        if not title_m:
            title_m = re.search(r'<h2[^>]*aria-label="([^"]+)"', block)
        price_m = re.search(r'<span class="a-offscreen">([^<]+)</span>', block)

        if not (asin_m and title_m):
            continue
        asin = asin_m[-1]
        if asin in seen:
            continue
        seen.add(asin)

        rating_m = re.search(r'([0-9.]+) out of 5 stars', block)
        title = _clean(title_m.group(1))
        sponsored = title.lower().startswith("sponsored")
        title = re.sub(r'^sponsored ad\s*-\s*', '', title, flags=re.I)

        items.append({
            "asin": asin,
            "brand": _brand(title),
            "title": title,
            "price": _clean(price_m.group(1)) if price_m else None,
            "price_value": _price_value(price_m.group(1)) if price_m else None,
            "rating": float(rating_m.group(1)) if rating_m else None,
            "ratings_total": _count(block),
            "delivery": _delivery(block),
            "sponsored": sponsored,
            "url": f"{BASE}/dp/{asin}",
        })
    return items


def _price_value(s: str):
    m = re.search(r'[\d,]+\.?\d*', s)
    return float(m.group(0).replace(",", "")) if m else None


def _count(block: str):
    """Extract a product's review count from a search-result block. The count
    lives in the rating span (data-rt=...), shown as '(2.3k)', '54', etc.
    A generic '(...)' match is unsafe — blocks contain rgba() colors."""
    m = re.search(r'data-rt="[0-9.]+"[^>]*>\s*\(?\s*([\d][\d.,]*)\s*([kKmM]?)\s*\)?\s*<',
                  block)
    if not m:
        m = re.search(r'by\s+([\d,]+)\s+reviews', block)
        return int(m.group(1).replace(",", "")) if m else None
    num = float(m.group(1).replace(",", ""))
    suffix = m.group(2).lower()
    if suffix == "k":
        num *= 1_000
    elif suffix == "m":
        num *= 1_000_000
    return int(num)


# Brands whose name is two words (title starts with these); everything else
# falls back to the first word of the title.
_MULTIWORD_BRANDS = (
    "western digital", "silicon power", "team group", "sk hynix",
    "g skill", "kingston technology", "seagate technology",
)


# Recognized brands (electronics/storage-focused). Titles start with the brand,
# so a result is kept only if its title begins with one of these. If NOTHING
# matches (e.g. a category this list doesn't cover), the filter is skipped so
# you never get an empty list. Edit freely to taste.
_KNOWN_BRANDS = (
    "samsung", "crucial", "western digital", "wd", "sandisk", "seagate",
    "kingston", "adata", "pny", "sabrent", "corsair", "sk hynix", "micron",
    "intel", "kioxia", "toshiba", "lexar", "patriot", "transcend", "teamgroup",
    "team group", "silicon power", "sp silicon power", "owc", "gigastone",
    "netac", "mushkin", "inland", "sabrent rocket", "hp", "asus", "acer",
)

# Off-type terms dropped unless the query itself asks for them.
_EXCLUDE = ("m.2", "nvme", "external", "portable", "enclosure")
# Query token -> extra title spellings that also count as a match.
_SYNONYMS = {"ssd": ("solidstatedrive",), "hdd": ("harddrive", "harddisk")}


def _norm(s: str) -> str:
    """Lowercase and strip everything but letters, digits, and dots."""
    return re.sub(r"[^a-z0-9.]", "", s.lower())


def _relevant(title: str, query: str) -> bool:
    """True if the title contains every meaningful query term and no
    off-type term the query didn't ask for."""
    nt = _norm(title)
    qn = _norm(query)
    for tok in query.split():
        t = _norm(tok)
        if len(t) < 2:
            continue
        if t in nt or any(s in nt for s in _SYNONYMS.get(t, ())):
            continue
        return False
    for bad in _EXCLUDE:
        if bad not in qn and bad in nt:
            return False
    return True


def _brand(title: str) -> str:
    low = title.lower()
    for b in _MULTIWORD_BRANDS:
        if low.startswith(b):
            return " ".join(title.split()[:2])
    return title.split()[0] if title else ""


def _known_brand(title: str) -> bool:
    """True if the title starts with a recognized brand name."""
    low = title.lower()
    return any(low == b or low.startswith(b + " ") for b in _KNOWN_BRANDS)


# The brand's home country (company HQ) — NOT where the unit was manufactured.
_BRAND_COUNTRY = {
    "samsung": "South Korea", "sk hynix": "South Korea",
    "crucial": "USA", "micron": "USA", "western digital": "USA", "wd": "USA",
    "sandisk": "USA", "seagate": "USA", "kingston": "USA", "intel": "USA",
    "corsair": "USA", "pny": "USA", "sabrent": "USA", "mushkin": "USA",
    "inland": "Taiwan", "owc": "USA", "patriot": "USA", "gigastone": "USA",
    "teamgroup": "Taiwan", "team group": "Taiwan", "sp": "Taiwan",
    "silicon power": "Taiwan", "transcend": "Taiwan", "adata": "Taiwan",
    "netac": "China", "lexar": "China", "fanxiang": "China",
    "ediloca": "China", "fikwot": "China", "kingspec": "China",
    "toshiba": "Japan", "kioxia": "Japan",
}


def _brand_country(title: str):
    """The brand's home country, matched from the start of the title."""
    low = title.lower()
    for b, country in _BRAND_COUNTRY.items():
        if low == b or low.startswith(b + " "):
            return country
    return None


# Inline note shown after the short brand: a fuller name or parent company.
_BRAND_NOTE = {"sp": "Silicon Power", "inland": "Micro Center"}


def _brand_note(title: str):
    """Fuller brand name / parent company, matched from the start of the title."""
    low = title.lower()
    for b, note in _BRAND_NOTE.items():
        if low == b or low.startswith(b + " "):
            return note
    return None


def cmd_search(args: argparse.Namespace) -> None:
    # A product URL or a bare ASIN (10 chars, letters+digits, no spaces) is a
    # single-product lookup, not a keyword search — route it to the detail view.
    is_asin = bool(re.fullmatch(r"[A-Z0-9]{10}", args.query)) and any(
        ch.isdigit() for ch in args.query)
    if args.query.startswith("http") or is_asin:
        args.identifier = args.query
        cmd_price(args)
        return

    # p_85:2470955011 is Amazon's "Prime Eligible" refinement — Amazon itself
    # returns only Prime items (reliable, unlike scraping per-item Prime status).
    q = urllib.parse.quote_plus(args.query)
    page = fetch(f"{BASE}/s?k={q}&rh=p_85%3A2470955011")
    items = parse_search(page)

    items = [r for r in items if _relevant(r["title"], args.query)]

    # Keep only recognized brands. Skip the filter if it would wipe out every
    # result (a category this brand list doesn't cover).
    branded = [r for r in items if _known_brand(r["title"])]
    if branded:
        items = branded

    # Fetch product pages for review count / model / delivery (the search page's
    # data is sparse). To limit requests, pre-sort by rating and enrich a bounded
    # pool of the best candidates, not every result, then keep 1000+ reviews.
    # (Prime is handled by Amazon's search filter above, not per-item.)
    MIN_REVIEWS = 1000
    items.sort(key=lambda r: (r.get("rating") is None, -(r.get("rating") or 0)))
    pool = items[: max(args.limit + 2, 10)]
    _enrich_counts(pool)
    kept = [r for r in pool if (r.get("ratings_total") or 0) >= MIN_REVIEWS]
    items = kept or pool

    if args.sort == "price":
        items.sort(key=lambda r: (r["price_value"] is None, r["price_value"] or 0))
        order = f"Prime, {MIN_REVIEWS:,}+ reviews, sorted by price (low to high)"
    elif args.sort == "delivery":
        items.sort(key=_delivery_key)
        order = f"Prime, {MIN_REVIEWS:,}+ reviews, sorted by delivery (fastest to slowest)"
    else:
        # Sort by rating (high to low), tie-broken by review count.
        items.sort(key=lambda r: (
            r.get("rating") is None,
            -(r.get("rating") or 0),
            -(r.get("ratings_total") or 0),
        ))
        order = f"Prime, {MIN_REVIEWS:,}+ reviews, sorted by rating (high to low)"

    items = items[: args.limit]

    if not items:
        print("No results parsed (Amazon markup may have changed, or a bot check hit).")
        return

    print(f"\n{c('Results for', '1')} {c(repr(args.query), '36')} "
          f"(showing {len(items)}, {order}):\n")
    # Highlights: for each metric the top two stand out (best bold, 2nd regular),
    # the rest white. Price green, rating cyan, review count blue, delivery yellow.
    dkeys = sorted({_delivery_key(r) for r in items if r.get("delivery")})
    fastest = dkeys[0] if dkeys else None
    second_fast = dkeys[1] if len(dkeys) > 1 else None
    prices = sorted({r["price_value"] for r in items if r.get("price_value") is not None})
    cheapest = prices[0] if prices else None
    second = prices[1] if len(prices) > 1 else None
    rated = sorted({r["rating"] for r in items if r.get("rating") is not None}, reverse=True)
    top_rating = rated[0] if rated else None
    second_rating = rated[1] if len(rated) > 1 else None
    counts = sorted({r["ratings_total"] for r in items if r.get("ratings_total") is not None},
                    reverse=True)
    most = counts[0] if counts else None
    second_most = counts[1] if len(counts) > 1 else None

    for i, r in enumerate(items, 1):
        pv = r.get("price_value")
        if r.get("price"):
            if pv is not None and pv == cheapest:
                price = c(r["price"], "92;1")   # cheapest: bold green
            elif pv is not None and pv == second:
                price = c(r["price"], "92")     # 2nd cheapest: green
            else:
                price = c(r["price"], "37")     # white
        else:
            price = c("n/a", "90")
        rt = r.get("rating")
        if rt is not None:
            rcol = "1;96" if rt == top_rating else "96" if rt == second_rating else "37"
            stars = c(f"{rt}★", rcol)
        else:
            stars = c("–", "90")
        n = r.get("ratings_total")
        if n is not None:
            ncol = "1;94" if n == most else "94" if n == second_most else "37"
            cnt = c(f"({n:,})", ncol)
        else:
            cnt = c("(n/a)", "90")
        spons = c(" ad", "90") if r.get("sponsored") else ""
        if r.get("delivery"):
            dk = _delivery_key(r)
            if dk == fastest:
                dcol = "1;93"   # fastest: bold yellow
            elif dk == second_fast:
                dcol = "93"     # 2nd fastest: yellow
            else:
                dcol = "37"     # white
            deliv = f"   {c('delivery ' + r['delivery'], dcol)}"
        else:
            deliv = ""
        note = _brand_note(r["title"])
        country = _brand_country(r["title"])
        name = r["brand"] + (f" {note}" if note else "")
        brand = c(name, "1") + (c(f" ({country})", "90") if country else "")
        print(f"{c(f'{i:>2}.', '90')} {brand}  {price}"
              f"   {stars} {cnt}{spons}{deliv}")
        print(f"    {r['title'][:92]}")
        model = f"{c('model ' + r['model'], '1')}   " if r.get("model") else ""
        print(f"    {model}{c(r['asin'], '90')}  {c(r['url'], '37')}\n")


def cmd_price(args: argparse.Namespace) -> None:
    ident = args.identifier
    if ident.startswith("http"):
        url = ident
    else:
        url = f"{BASE}/dp/{ident}"
    page = fetch(url)

    title_m = re.search(r'id="productTitle"[^>]*>([^<]+)<', page)
    price_m = (re.search(r'"priceToPay".*?<span class="a-offscreen">([^<]+)</span>',
                         page, re.S)
               or re.search(r'id="corePrice[^"]*".*?<span class="a-offscreen">([^<]+)</span>',
                            page, re.S)
               or re.search(r'<span class="a-offscreen">([^<]+)</span>', page))
    rating_m = re.search(r'([0-9.]+) out of 5 stars', page)
    avail_m = re.search(r'id="availability".*?<span[^>]*>([^<]+)</span>', page, re.S)
    asin_m = re.search(r'"asin"\s*:\s*"([A-Z0-9]{10})"', page) or \
        re.search(r'/dp/([A-Z0-9]{10})', url)

    if not title_m:
        sys.exit("Could not find product (bad ASIN/URL, or a bot check hit).")

    result = {
        "asin": asin_m.group(1) if asin_m else None,
        "title": _clean(title_m.group(1)),
        "price": _clean(price_m.group(1)) if price_m else None,
        "rating": float(rating_m.group(1)) if rating_m else None,
        "availability": _clean(avail_m.group(1)) if avail_m else None,
        "delivery": _delivery(page),
        "url": url,
    }

    print(f"\n{c(result['title'], '1')}")
    print(f"Price:   {c(result['price'] or 'n/a', '32;1')}")
    if result["rating"] is not None:
        print(f"Rating:  {result['rating']}★")
    if result["availability"]:
        print(f"Stock:   {result['availability']}")
    if result["delivery"]:
        print(f"Deliver: {result['delivery']}")
    print(f"ASIN:    {result['asin']}")
    print(f"URL:     {result['url']}\n")


EXAMPLES = f"""\
{c('amazon-cli', '1')} — search Amazon and check prices from the terminal

{c('Examples:', '1')}
  {c('amazon-cli "4TB 2.5 SATA Internal SSD"', '36')}             sorted by rating (default)
  {c('amazon-cli "4TB 2.5 SATA Internal SSD" --rating', '36')}    sorted by rating (high to low)
  {c('amazon-cli "4TB 2.5 SATA Internal SSD" --price', '36')}     sorted by price (low to high)
  {c('amazon-cli "4TB 2.5 SATA Internal SSD" --delivery', '36')}  sorted by delivery (fastest to slowest)

All examples — Prime, top 10 brands, 1000+ reviews.
Run {c('amazon-cli -h', '90')} for all options.
"""


def show_examples() -> None:
    print(EXAMPLES)


def main() -> None:
    # Bare `amazon` or `amazon help` -> friendly examples (no error). `-h` falls
    # through to argparse's full option listing below.
    if len(sys.argv) == 1 or sys.argv[1] == "help":
        show_examples()
        return

    parser = argparse.ArgumentParser(
        prog="amazon-cli",
        description="Search Amazon and check prices from the CLI. "
                    "Give keywords, or an ASIN / product URL for a single lookup.",
    )
    parser.add_argument("query", help="Search keywords, or an ASIN / amazon.com URL")
    parser.add_argument("-n", "--limit", type=int, default=10, help="Max results (default 10)")
    sort = parser.add_mutually_exclusive_group()
    sort.add_argument("--rating", dest="sort", action="store_const", const="reviews",
                      help="Sort by rating (high to low) — default")
    sort.add_argument("--price", dest="sort", action="store_const", const="price",
                      help="Sort by price (low to high)")
    sort.add_argument("--delivery", dest="sort", action="store_const", const="delivery",
                      help="Sort by delivery (fastest to slowest)")
    parser.set_defaults(sort="reviews")

    args = parser.parse_args()
    cmd_search(args)


if __name__ == "__main__":
    main()
