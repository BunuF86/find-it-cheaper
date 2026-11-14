#!/usr/bin/env python3
"""Build the interactive "מצא לי כזה בזול" report from a verified report.json.

    python3 build_report.py report.json -o report.html

Validates the data against the honesty rules of the skill (unknown shipping is
never free, no total without known shipping, no "cheapest" across currencies),
embeds the images, and writes one self-contained HTML file.
Standard library only. Schema: references/report-schema.md
"""
import argparse
import base64
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "report-template.html"
PLACEHOLDER = "/*__REPORT_DATA__*/"
MATCH_TYPES = ("exact", "similar", "unconfirmed")
SHIPPING = ("known", "free", "unknown", "unavailable")
AVAILABILITY = ("in_stock", "out_of_stock", "unknown")
INPUT_TYPES = ("image", "link", "text", "barcode")
REJECT_REASONS = ("blocked", "different_product", "out_of_stock", "used", "conditional_price", "duplicate",
                  "no_shipping", "not_a_shop", "other")
MAX_OFFERS = 12
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
WINDOW_TITLE_TAIL = "מצא לי כזה בזול"  # the report's <title> ends with this; used to raise its window
MAX_IMAGE_BYTES = 3 * 1024 * 1024
IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF8", "image/gif"),
)


def sniff_image(data):
    for magic, mime in IMAGE_MAGIC:
        if data.startswith(magic):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    return None


def is_http_url(value):
    if not isinstance(value, str):
        return False
    parts = urllib.parse.urlsplit(value.strip())
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def is_money(value):
    return (
        isinstance(value, dict)
        and isinstance(value.get("amount"), (int, float))
        and not isinstance(value.get("amount"), bool)
        and value["amount"] > 0
        and isinstance(value.get("currency"), str)
        and re.fullmatch(r"[A-Z]{3}", value["currency"]) is not None
    )


def validate(data):
    """Return a list of human-readable problems; empty means the report may be built."""
    errors = []
    if not isinstance(data, dict):
        return ["report must be a JSON object"]

    query = data.get("query")
    if not isinstance(query, dict) or not str(query.get("text") or "").strip():
        errors.append("query.text is required (what the user asked for)")
    elif query.get("input_type") not in INPUT_TYPES:
        errors.append(f"query.input_type must be one of {INPUT_TYPES}")

    product = data.get("product")
    if not isinstance(product, dict) or not str(product.get("title") or "").strip():
        errors.append("product.title is required")

    checked = data.get("checked_at")
    try:
        date.fromisoformat(str(checked))
    except ValueError:
        errors.append("checked_at must be the date prices were read, as YYYY-MM-DD")

    offers = data.get("offers")
    if not isinstance(offers, list):
        return errors + ["offers must be a list (an empty list is a valid honest result)"]
    if len(offers) > MAX_OFFERS:
        errors.append(f"too many offers ({len(offers)}); keep the best {MAX_OFFERS} or fewer")

    seen = set()
    for i, o in enumerate(offers):
        where = f"offers[{i}]"
        if not isinstance(o, dict):
            errors.append(f"{where} must be an object")
            continue
        oid = o.get("id")
        if oid is None or str(oid) in seen:
            errors.append(f"{where}.id is required and must be unique")
        seen.add(str(oid))
        for field in ("store", "title"):
            if not str(o.get(field) or "").strip():
                errors.append(f"{where}.{field} is required")
        if not is_http_url(o.get("url")):
            errors.append(f"{where}.url must be the real http(s) product page that was opened")
        mt = o.get("match_type")
        if mt not in MATCH_TYPES:
            errors.append(f"{where}.match_type must be one of {MATCH_TYPES}")
        if not str(o.get("match_reason") or "").strip():
            errors.append(f"{where}.match_reason is required: why it is {mt}")
        if mt == "similar" and not str(o.get("differences") or "").strip():
            errors.append(f"{where}.differences is required for a similar offer")

        price = o.get("price")
        if price is not None and not is_money(price):
            errors.append(f"{where}.price must be {{amount > 0, currency: 'ILS'}} or null if not verified")
        if price is None and mt != "unconfirmed":
            errors.append(f"{where}: an offer without a verified price must be match_type 'unconfirmed'")

        ship = o.get("shipping")
        status = ship.get("status") if isinstance(ship, dict) else None
        if status not in SHIPPING:
            errors.append(f"{where}.shipping.status must be one of {SHIPPING} (use 'unknown', never assume free)")
        elif status == "known" and not is_money(ship):
            errors.append(f"{where}.shipping: status 'known' needs amount and currency")
        elif status == "free" and not str(ship.get("note") or "").strip():
            errors.append(f"{where}.shipping: status 'free' needs a note quoting where the shop says so")
        elif status in ("unknown", "unavailable") and ship.get("amount") is not None:
            errors.append(f"{where}.shipping: no amount when status is '{status}'")

        total = o.get("total")
        if total is not None:
            if not is_money(total):
                errors.append(f"{where}.total must be {{amount, currency}} or null")
            elif status not in ("known", "free"):
                errors.append(f"{where}.total is not allowed while shipping is '{status}'")
            elif not is_money(price) or total["currency"] != price["currency"]:
                errors.append(f"{where}.total must be in the same currency as a verified price")
            else:
                floor = price["amount"]
                if status == "known" and ship["currency"] == price["currency"]:
                    floor += ship["amount"]
                if total["amount"] < floor - 0.005:
                    errors.append(f"{where}.total is lower than price + shipping")

        if o.get("availability") not in AVAILABILITY:
            errors.append(f"{where}.availability must be one of {AVAILABILITY}")
        if o.get("evidence") is not None and not (
            isinstance(o["evidence"], list) and all(isinstance(e, str) for e in o["evidence"])
        ):
            errors.append(f"{where}.evidence must be a list of short strings")

    ref = (query or {}).get("reference_price") if isinstance(query, dict) else None
    if ref is not None and not is_money(ref):
        errors.append("query.reference_price must be {amount, currency}: the price the user saw, or omit it")
    rejected = data.get("rejected", [])
    if not isinstance(rejected, list) or not all(
        isinstance(r, dict) and str(r.get("store") or "").strip() and r.get("why") in REJECT_REASONS for r in rejected
    ):
        errors.append(f"rejected must be a list of {{store, why}} with why in {REJECT_REASONS}: listings really seen in this run and dropped")

    pick = data.get("top_pick")
    if pick is not None and not errors:
        errors += validate_top_pick(pick, offers)
    return errors


def validate_top_pick(pick, offers):
    """The 'cheapest verified' ribbon is only allowed when the numbers prove it."""
    if not isinstance(pick, dict) or pick.get("basis") not in ("total", "item_price"):
        return ["top_pick must be {offer_id, basis: 'total' | 'item_price'} or omitted"]
    chosen = next((o for o in offers if str(o.get("id")) == str(pick.get("offer_id"))), None)
    if chosen is None:
        return ["top_pick.offer_id does not match any offer"]
    if chosen["match_type"] == "unconfirmed":
        return ["top_pick cannot be an unconfirmed offer"]
    if chosen["availability"] == "out_of_stock":
        return ["top_pick cannot be out of stock"]
    # Compare only like with like: same match type, available, verified price.
    group = [
        o for o in offers
        if o["match_type"] == chosen["match_type"] and o["availability"] != "out_of_stock" and o.get("price")
    ]
    field = "total" if pick["basis"] == "total" else "price"
    if pick["basis"] == "total" and not chosen.get("total"):
        return ["top_pick basis 'total' needs a verified total on the chosen offer"]
    if pick["basis"] == "item_price" and any(o.get("total") for o in group):
        return ["top_pick basis 'item_price' is misleading while some offers have a full total; use 'total' or omit"]
    rivals = [o for o in group if o.get(field)] if pick["basis"] == "total" else group
    currencies = {o[field]["currency"] for o in rivals}
    if len(currencies) > 1:
        return [f"top_pick: offers are in different currencies {sorted(currencies)}; no cheapest can be declared"]
    lowest = min(o[field]["amount"] for o in rivals)
    if chosen[field]["amount"] > lowest + 0.005:
        return ["top_pick is not the lowest comparable offer"]
    return []


def host_is_public(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


def to_data_uri(data, source):
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"image is larger than {MAX_IMAGE_BYTES // 1024 // 1024}MB: {source}")
    mime = sniff_image(data)
    if not mime:
        raise ValueError(f"not a JPEG/PNG/WebP/GIF/AVIF image: {source}")
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def embed_image(value, base_dir, fetch, warnings, referer=None):
    """Local path or https URL -> data URI. A failed download keeps the URL; a bad value becomes None."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.startswith("data:image/"):
        return value
    parts = urllib.parse.urlsplit(value)
    if parts.scheme == "https":
        if not fetch:
            return value
        try:
            if not host_is_public(parts.hostname or ""):
                raise ValueError("host is not public")
            headers = {"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8"}
            if referer:  # many shops only serve product images to requests coming from their own pages
                headers["Referer"] = referer
            req = urllib.request.Request(value, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                return to_data_uri(resp.read(MAX_IMAGE_BYTES + 1), value)
        except Exception as exc:  # network, size, or type problem: the template shows a placeholder if it 404s
            warnings.append(f"image not embedded, left as a link ({exc}): {value}")
            return value
    if parts.scheme or value.startswith("//"):
        warnings.append(f"image dropped, only https or local files are allowed: {value}")
        return None
    path = (base_dir / value).resolve()
    try:
        return to_data_uri(path.read_bytes(), str(path))
    except (OSError, ValueError) as exc:
        warnings.append(f"image dropped ({exc})")
        return None


def embed_json(data):
    # Safe inside <script type="application/json">: no "</script>" or "<!--" can appear.
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), (" ", "\\u2028"), (" ", "\\u2029")):
        text = text.replace(ch, esc)
    return text


APP_FLAGS = ["--new-window", "--start-maximized", "--no-first-run", "--no-default-browser-check",
             "--autoplay-policy=no-user-gesture-required"]


def _profile_dir():
    """A browser profile of our own: a separate process, so the window and autoplay flags really apply."""
    root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    path = os.path.join(root, "find-it-cheaper", "browser")
    os.makedirs(path, exist_ok=True)
    return path


def _mac_app_window(path):
    for app in ("Google Chrome", "Microsoft Edge"):
        if os.path.isdir(f"/Applications/{app}.app"):
            subprocess.run(["open", "-na", app, "--args", *APP_FLAGS, "--user-data-dir=" + _profile_dir(),
                            "--app=" + Path(path).as_uri()], check=True, timeout=15)
            return True
    return False


def _windows_app_window(path):
    """Windows: open the report in its own Chrome/Edge window and pull it to the front.

    A plain "open file" only adds a tab to a browser that may sit behind other windows,
    so the user never sees the report arrive.
    """
    roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    exes = [os.path.join(r, *parts) for parts in (("Google", "Chrome", "Application", "chrome.exe"),
                                                  ("Microsoft", "Edge", "Application", "msedge.exe"))
            for r in roots if r]
    exe = next((e for e in exes if os.path.isfile(e)), None)
    if not exe:
        return False
    subprocess.Popen([exe, *APP_FLAGS, "--user-data-dir=" + _profile_dir(), "--app=" + Path(path).as_uri()],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Windows refuses focus changes from background processes; a synthetic Alt press lifts that lock.
    script = ("Start-Sleep -Milliseconds 1200; $w = New-Object -ComObject WScript.Shell; "
              "$w.SendKeys('%'); $null = $w.AppActivate('" + WINDOW_TITLE_TAIL + "')")
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass  # the window is open even if it could not be raised
    return True


def open_in_browser(path):
    """Put the finished report on the user's screen. Returns True only if a launcher was started."""
    path = str(Path(path).resolve())
    try:
        if sys.platform.startswith("win"):
            if _windows_app_window(path):
                return True
            os.startfile(path)  # noqa: S606 - opening the user's own report
            return True
        if sys.platform == "darwin":
            if _mac_app_window(path):
                return True
            subprocess.run(["open", path], check=True, timeout=15)  # `open` also activates the browser
            return True
        if "microsoft" in platform.release().lower():  # WSL: hand the file to Windows
            win = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True, timeout=10).stdout.strip()
            if win and shutil.which("explorer.exe"):
                subprocess.Popen(["explorer.exe", win])
                return True
        if shutil.which("xdg-open") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:
        pass
    return False


def utf8_console():
    # Windows consoles default to a legacy code page and crash or garble Hebrew output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    utf8_console()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("report", type=Path, help="report.json produced after verification (with --open: the built .html)")
    ap.add_argument("--open", action="store_true", help="only open an already built report.html on the user's screen")
    ap.add_argument("-o", "--output", type=Path, help="output HTML (default: next to the JSON)")
    ap.add_argument("--no-fetch", action="store_true", help="do not download remote images; keep them as links")
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    ap.add_argument("--allow-missing-images", action="store_true", help="tests only: build even if an offer has no picture")
    ap.add_argument("--no-open", action="store_true", help="do not open the report in the browser when it is built")
    args = ap.parse_args()

    if args.open:
        if not args.report.is_file():
            sys.exit(f"no such file: {args.report}")
        opened = open_in_browser(args.report)
        print(("OPENED on the user's screen: " if opened else "NOT OPENED automatically. Ask the user to open this file: ") + str(args.report.resolve()))
        return

    try:
        data = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"cannot read {args.report}: {exc}")

    errors = validate(data)
    if errors:
        print("Report rejected. Fix the data, do not work around the check:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)
    if args.check:
        print("OK")
        return

    warnings = []
    base = args.report.resolve().parent
    fetch = not args.no_fetch
    data["product"]["image"] = embed_image(data["product"].get("image"), base, fetch, warnings)
    for o in data["offers"]:
        o["image"] = embed_image(o.get("image"), base, fetch, warnings, referer=o.get("url"))
    for r in data.get("rejected", []):  # thumbnails for the opening animation only: keep as https links, never embed
        if not (isinstance(r.get("image"), str) and r["image"].startswith("https://")):
            r.pop("image", None)

    # No card without a picture. An image that is really inside the file beats a link that may not load.
    def solid(img):
        return isinstance(img, str) and (img.startswith("data:image/") or (not fetch and img.startswith("https://")))

    exact_pool = [o["image"] for o in data["offers"] if o["match_type"] == "exact" and solid(o["image"])]
    product_img = data["product"]["image"] if solid(data["product"]["image"]) else None
    missing = []
    for i, o in enumerate(data["offers"]):
        if solid(o["image"]):
            continue
        # The same exact product looks the same in every shop, so another shop's picture of it is honest.
        fallback = (exact_pool[0] if exact_pool else product_img) if o["match_type"] in ("exact", "unconfirmed") else None
        if fallback:
            o["image"] = fallback
        else:
            missing.append(f"offers[{i}] ({o['store']}: {o['title']})")
    if not solid(data["product"]["image"]):
        pool = exact_pool or [o["image"] for o in data["offers"] if solid(o["image"])]
        if pool:
            data["product"]["image"] = pool[0]
        elif data["offers"]:
            missing.append("product.image")
    if missing and not args.allow_missing_images:
        print("Report not built: every offer needs a picture that actually loads.", file=sys.stderr)
        for m in missing:
            print(f"  - no usable image for {m}", file=sys.stderr)
        for w in warnings:
            print(f"  ({w})", file=sys.stderr)
        print("Find another image URL of that same product (the shop's og:image, another shop that sells it, "
              "or the manufacturer's page), put it in the offer's `image`, and build again.", file=sys.stderr)
        sys.exit(1)

    template = TEMPLATE.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        sys.exit(f"template is missing the {PLACEHOLDER} marker: {TEMPLATE}")
    out = args.output or args.report.with_suffix(".html")
    out.write_text(template.replace(PLACEHOLDER, embed_json(data)), encoding="utf-8")
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"{out} ({out.stat().st_size // 1024} KB, {len(data['offers'])} offers)")
    if args.no_open:
        return
    if open_in_browser(out):
        print(f"OPENED on the user's screen: {out.resolve()}")
    else:
        print(f"NOT OPENED automatically. Ask the user to open this file: {out.resolve()}")


if __name__ == "__main__":
    main()
