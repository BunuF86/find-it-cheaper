#!/usr/bin/env python3
"""First-pass check of many candidate product pages at once.

    python3 check_pages.py urls.txt -o candidates.json
    python3 check_pages.py https://shop.example/p/1 https://other.example/x

Fetches every URL in parallel and reads what the shop itself publishes in the
page: schema.org Product data (name, price, currency, availability, image,
GTIN/SKU/brand) and Open Graph tags. Nothing is guessed: a field that is not in
the page stays null, and a page that blocks the request is reported as blocked.
Standard library only.

The output is evidence for the agent, not a verdict. The agent still decides
whether it is the same product, and opens pages that came back incomplete.
"""
import argparse
import gzip
import html
import json
import re
import sys
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
MAX_BYTES = 3_000_000
AVAILABILITY = {
    "instock": "in_stock", "onlineonly": "in_stock", "limitedavailability": "in_stock", "instoreonly": "in_stock",
    "outofstock": "out_of_stock", "soldout": "out_of_stock", "discontinued": "out_of_stock",
}


class Head(HTMLParser):
    """Collects JSON-LD blocks, meta tags and the title."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta, self.ld, self.title = {}, [], ""
        self._ld = self._title = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()
        elif tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._ld, self._buf = True, []
        elif tag == "title" and not self.title:
            self._title, self._buf = True, []

    def handle_data(self, data):
        if self._ld or self._title:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._ld:
            self.ld.append("".join(self._buf))
            self._ld = False
        elif tag == "title" and self._title:
            self.title = " ".join("".join(self._buf).split())
            self._title = False


def walk(node):
    """Yield every dict inside a JSON-LD structure (handles @graph and nesting)."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def is_type(node, name):
    t = node.get("@type")
    return name in (t if isinstance(t, list) else [t])


def to_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^\d.,]", "", str(value))
    if not s:
        return None
    if "," in s and "." in s:  # 1,299.00 or 1.299,00
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def first(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or value.get("name")
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse(page_url, text):
    head = Head()
    try:
        head.feed(text)
    except Exception:
        pass
    out = {"title": None, "price": None, "currency": None, "availability": None, "image": None,
           "brand": None, "sku": None, "gtin": None, "offer_count": 0, "price_source": None}

    products = []
    for block in head.ld:
        try:
            data = json.loads(block.strip(), strict=False)
        except ValueError:
            continue
        products += [n for n in walk(data) if is_type(n, "Product") or is_type(n, "ProductGroup")]
    for prod in products:
        out["title"] = out["title"] or first(prod.get("name"))
        out["image"] = out["image"] or first(prod.get("image"))
        out["brand"] = out["brand"] or first(prod.get("brand"))
        out["sku"] = out["sku"] or first(prod.get("sku")) or first(prod.get("mpn"))
        for key in ("gtin13", "gtin", "gtin12", "gtin14", "gtin8"):
            out["gtin"] = out["gtin"] or (str(prod[key]).strip() if prod.get(key) else None)
        offers = [n for n in walk(prod.get("offers")) if n.get("price") is not None or n.get("lowPrice") is not None]
        out["offer_count"] = max(out["offer_count"], len(offers))
        if offers and out["price"] is None:
            # Several variants on one page: report the lowest and say so, the agent must check the variant.
            priced = [(to_number(o.get("price", o.get("lowPrice"))), o) for o in offers]
            priced = [(p, o) for p, o in priced if p]
            if priced:
                price, offer = min(priced, key=lambda x: x[0])
                out["price"], out["currency"] = price, first(offer.get("priceCurrency"))
                out["price_source"] = "json-ld" if len(priced) == 1 else f"json-ld, lowest of {len(priced)} variants"
                avail = str(offer.get("availability") or "").rsplit("/", 1)[-1].lower()
                out["availability"] = AVAILABILITY.get(avail)

    m = head.meta
    if out["price"] is None:
        price = to_number(m.get("product:price:amount") or m.get("og:price:amount") or m.get("price"))
        if price:
            out["price"], out["price_source"] = price, "meta tag"
            out["currency"] = m.get("product:price:currency") or m.get("og:price:currency") or m.get("pricecurrency")
    if out["availability"] is None:
        avail = (m.get("product:availability") or m.get("og:availability") or "").replace(" ", "").lower()
        out["availability"] = AVAILABILITY.get(avail)
    out["title"] = out["title"] or m.get("og:title") or head.title or None
    out["image"] = out["image"] or m.get("og:image") or m.get("twitter:image")
    if out["image"]:
        out["image"] = urllib.parse.urljoin(page_url, html.unescape(out["image"]))
        if not out["image"].startswith("https://"):
            out["image"] = None
    if out["currency"]:
        out["currency"] = out["currency"].upper()[:3]
    if out["title"]:
        out["title"] = " ".join(html.unescape(out["title"]).split())[:200]
    return out


def repair(url):
    """Some comparison sites hand out links whose Hebrew path was UTF-8 encoded twice."""
    if "%C3%97" not in url.upper():
        return url
    try:
        fixed = urllib.parse.unquote(url, encoding="latin-1").encode("latin-1").decode("utf-8")
        return urllib.parse.quote(fixed, safe=":/?&=%#")
    except (UnicodeError, ValueError):
        return url


def check(url):
    url = repair(url)
    host = urllib.parse.urlsplit(url).hostname or ""
    result = {"url": url, "store_host": host.removeprefix("www."), "status": "error", "http": None, "note": None}
    if urllib.parse.urlsplit(url).scheme not in ("http", "https") or not host:
        result["note"] = "not an http(s) url"
        return result
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "he-IL,he;q=0.9,en;q=0.8", "Accept-Encoding": "gzip, deflate"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result["http"] = resp.status
            raw = resp.read(MAX_BYTES)
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
            charset = resp.headers.get_content_charset() or "utf-8"
            final = resp.geturl()
    except urllib.error.HTTPError as exc:
        result["http"] = exc.code
        result["status"] = "blocked" if exc.code in (401, 403, 429, 503) else "error"
        result["note"] = f"HTTP {exc.code}"
        return result
    except Exception as exc:
        result["note"] = str(exc)[:120]
        return result
    text = raw.decode(charset, errors="replace")
    result.update(parse(final, text))
    if final != url:  # e.g. a comparison-site link that redirects to the shop: report the shop
        result["final_url"] = final
        result["store_host"] = (urllib.parse.urlsplit(final).hostname or host).removeprefix("www.")
    if result["price"] is not None:
        result["status"] = "ok"
    elif result["http"] == 202 or len(text) < 1500:
        result["status"], result["note"] = "blocked", "the shop served a bot-check stub instead of the page"
    elif re.search(r"captcha|cf-challenge|just a moment|access denied|are you a robot", text[:20000], re.I):
        result["status"], result["note"] = "blocked", "bot check page"
    else:
        result["status"], result["note"] = "no_price_data", "page loaded but publishes no machine-readable price; open it and read it"
    return result


def main():
    for stream in (sys.stdout, sys.stderr):  # Windows consoles garble Hebrew without this
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("urls", nargs="+", help="product page URLs, or one text file with a URL per line")
    ap.add_argument("-o", "--output", help="write JSON here (default: stdout)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    urls = args.urls
    if len(urls) == 1 and not urls[0].startswith("http"):
        with open(urls[0], encoding="utf-8") as fh:
            urls = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    urls = list(dict.fromkeys(urls))
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 16))) as pool:
        results = list(pool.map(check, urls))

    text = json.dumps(results, ensure_ascii=False, indent=1)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        counts = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print(f"{args.output}: {len(results)} pages, " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    else:
        print(text)


if __name__ == "__main__":
    main()
