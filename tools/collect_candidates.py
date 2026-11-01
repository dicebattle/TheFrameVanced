#!/usr/bin/env python3
"""Collect wide, high-resolution candidates from open image sources.

Usage: python tools/collect_candidates.py OUTPUT_FOLDER [SOURCE ...]
Sources: commons, skylines, met, cleveland, nga, nasa (default: all).

Writes candidates.json and numbered contact sheets, so that a person can pick
the images worth showing. Nothing here decides what goes on the TV.
"""
import csv
import html
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

UA = "TheFrameVanced/0.1"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
SIZE = "filew:>3800 fileh:>1900"
FONT = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
COLS, ROWS, TILE_W, TILE_H = 5, 6, 372, 209
PAINTING_TOPICS = ["landscape", "snow", "sunset", "moonlight", "river", "sea", "mountain", "autumn", "harbor", "garden"]
NGA_ARTISTS = (
    "Monet", "Gogh", "Turner", "Bierstadt", "Church", "Pissarro", "Sisley", "Renoir", "Corot", "Cole",
    "Inness", "Homer", "Hassam", "Kensett", "Canaletto", "Cézanne", "Constable", "Moran", "Heade", "Whistler",
)
thumbs = {}


def fetch(url):
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            wait = int(e.headers.get("Retry-After") or 30)
            print(f"rate limited, waiting {wait}s", flush=True)
            time.sleep(wait + 5 * attempt)
    raise RuntimeError("still rate limited")


def fetch_json(url, **params):
    return json.loads(fetch(url + ("?" + urllib.parse.urlencode(params) if params else "")))


def strip(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def wide_enough(w, h, lo=1.2, hi=2.3):
    return h > 0 and lo <= w / h <= hi


def row(group, provider, title, url, thumb, w, h, license_name, artist):
    return {
        "group": group, "provider": provider, "title": title, "url": url, "thumb": thumb,
        "w": w, "h": h, "license": license_name, "artist": artist[:80],
    }


def commons_query(group, query, limit, seen, rows, lo=1.45, hi=2.1, minw=3800):
    data = fetch_json(
        COMMONS_API, action="query", format="json", generator="search", gsrnamespace=6, gsrsearch=query,
        gsrlimit=limit, prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=500,
        iiextmetadatafilter="LicenseShortName|Artist",
    )
    for page in data.get("query", {}).get("pages", {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        w, h = info.get("width", 0), info.get("height", 0)
        if page["title"] in seen or info.get("mime") != "image/jpeg" or w < minw or not wide_enough(w, h, lo, hi):
            continue
        seen.add(page["title"])
        meta = info.get("extmetadata", {})
        rows.append(row(
            group, "Wikimedia Commons", page["title"].removeprefix("File:"), info["url"], info["thumburl"], w, h,
            strip(meta.get("LicenseShortName", {}).get("value")), strip(meta.get("Artist", {}).get("value")),
        ))


def commons(rows):
    seen = set()
    for group, category in (
        ("landscape", "landscapes"), ("city", "cityscapes"), ("city", "skylines"),
        ("aerial", "from unmanned aerial vehicles"), ("golden", "twilights"), ("golden", "sunsets"),
        ("golden", "sunrises"), ("golden", "aurora"),
    ):
        commons_query(group, f'incategory:"Featured pictures of {category}" {SIZE}'.replace("of from", "from"), 40, seen, rows)
        time.sleep(2)


def skylines(rows):
    seen = set()
    places = (
        "Hong Kong skyline night", "Tokyo skyline night", "Shanghai Pudong night", "Singapore Marina Bay night",
        "Manhattan skyline night", "Chicago skyline night", "Dubai Marina night", "San Francisco skyline night",
        "Paris Eiffel Tower night aerial", "London skyline night", "Sydney Harbour night", "Seoul night skyline",
        "Busan Haeundae night", "aerial view city night", "drone city at night",
    )
    for place in places:
        commons_query("skyline", f"{place} filetype:bitmap {SIZE}", 16, seen, rows)
        time.sleep(2)


def met(rows):
    api = "https://collectionapi.metmuseum.org/public/collection"
    seen = set()
    for topic in PAINTING_TOPICS:
        found = fetch_json(f"{api}/v1.1/search", q=topic, hasImages="true", isPublicDomain="true", limit=40)
        for object_id in (found.get("objectIDs") or [])[:40]:
            if object_id in seen:
                continue
            seen.add(object_id)
            try:
                item = fetch_json(f"{api}/v1/objects/{object_id}")
            except urllib.error.HTTPError:
                continue
            time.sleep(0.05)
            if not item.get("isPublicDomain") or "Painting" not in (item.get("classification") or ""):
                continue
            small, full = item.get("primaryImageSmall"), item.get("primaryImage")
            if not small or not full:
                continue
            try:
                thumbs[small] = Image.open(io.BytesIO(fetch(small))).convert("RGB")
            except (OSError, urllib.error.URLError):
                continue
            w, h = thumbs[small].size
            if wide_enough(w, h):
                rows.append(row("met", "The Metropolitan Museum of Art", item["title"], full, small, w, h, "CC0", item.get("artistDisplayName") or ""))


def cleveland(rows):
    seen = set()
    for topic in PAINTING_TOPICS:
        found = fetch_json(
            "https://openaccess-api.clevelandart.org/api/artworks/", q=topic, cc0=1, has_image=1, type="Painting",
            limit=40, fields="id,title,creators,images,share_license_status",
        )
        for item in found.get("data", []):
            images = item.get("images") or {}
            files = [i for i in images.values() if isinstance(i, dict) and i.get("url")]
            best = max(files, key=lambda i: int(i.get("width") or 0), default=None)
            web = images.get("web") or {}
            if item["id"] in seen or not best or not web.get("url"):
                continue
            seen.add(item["id"])
            w, h = int(best["width"]), int(best["height"])
            if w >= 2800 and wide_enough(w, h):
                creators = item.get("creators") or [{}]
                rows.append(row("cleveland", "Cleveland Museum of Art", item["title"], best["url"], web["url"], w, h, "CC0", creators[0].get("description") or ""))
        time.sleep(0.3)


def nga(rows):
    base = "https://raw.githubusercontent.com/NationalGalleryOfArt/opendata/main/data"
    objects = {}
    for item in csv.DictReader(io.StringIO(fetch(f"{base}/objects.csv").decode("utf-8"))):
        artist = item.get("attribution") or ""
        if item.get("classification") == "Painting" and any(name in artist for name in NGA_ARTISTS):
            objects[item["objectid"]] = item
    for image in csv.DictReader(io.StringIO(fetch(f"{base}/published_images.csv").decode("utf-8"))):
        item = objects.get(image.get("depictstmsobjectid"))
        w, h = int(image.get("width") or 0), int(image.get("height") or 0)
        # A number in maxpixels marks an image restricted by copyright.
        if not item or image.get("viewtype") != "primary" or image.get("maxpixels") or w < 3800 or not wide_enough(w, h):
            continue
        rows.append(row(
            "nga", "National Gallery of Art", item["title"], f'{image["iiifurl"]}/full/!3840,3840/0/default.jpg',
            f'{image["iiifurl"]}/full/!500,500/0/default.jpg', w, h, "CC0", item["attribution"],
        ))


def nasa(rows):
    seen = set()
    for query in (
        "city lights at night International Space Station", "night view city Earth observation", "aurora from International Space Station",
        "sunrise over Earth International Space Station", "Korea night Earth observation", "Tokyo night Earth observation",
        "Earth limb sunset space station", "Himalayas from space station",
    ):
        found = fetch_json("https://images-api.nasa.gov/search", q=query, media_type="image", page_size=40)
        for entry in found["collection"]["items"]:
            data = entry["data"][0]
            nasa_id = data["nasa_id"]
            links = entry.get("links") or []
            if nasa_id in seen or not links:
                continue
            seen.add(nasa_id)
            thumb = links[0]["href"]
            try:
                thumbs[thumb] = Image.open(io.BytesIO(fetch(thumb))).convert("RGB")
                assets = fetch_json(entry["href"])
            except (OSError, ValueError, urllib.error.URLError):
                continue
            full = next((a for a in assets if "~orig." in a and a.lower().endswith((".jpg", ".jpeg"))), None)
            w, h = thumbs[thumb].size
            if full and wide_enough(w, h, 1.3, 2.1):
                rows.append(row("nasa", "NASA", data.get("title", nasa_id), full.replace("http://", "https://"), thumb, w, h, "Public domain (NASA)", data.get("photographer") or "NASA"))
        time.sleep(0.5)


def contact_sheets(rows, out):
    font = ImageFont.truetype(FONT, 30)
    groups = {}
    for r in rows:
        groups.setdefault(r["group"], []).append(r)
    number = 0
    for group, items in groups.items():
        page = COLS * ROWS
        for start in range(0, len(items), page):
            sheet = Image.new("RGB", (COLS * TILE_W, ROWS * TILE_H), "black")
            draw = ImageDraw.Draw(sheet)
            for i, r in enumerate(items[start : start + page]):
                number += 1
                r["id"] = number
                try:
                    image = thumbs.get(r["thumb"]) or Image.open(io.BytesIO(fetch(r["thumb"]))).convert("RGB")
                except (OSError, ValueError, RuntimeError, urllib.error.URLError) as e:
                    r["thumb_error"] = str(e)[:80]
                    continue
                image = image.copy()
                image.thumbnail((TILE_W - 4, TILE_H - 4))
                x, y = (i % COLS) * TILE_W + 2, (i // COLS) * TILE_H + 2
                sheet.paste(image, (x, y))
                draw.rectangle((x, y, x + 76, y + 38), fill="black")
                draw.text((x + 6, y - 3), str(number), fill="yellow", font=font)
                if r["provider"] == "Wikimedia Commons":
                    time.sleep(1.2)
            sheet.save(out / f"sheet_{group}_{start // page + 1}.jpg", quality=82)


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    sources = {"commons": commons, "skylines": skylines, "met": met, "cleveland": cleveland, "nga": nga, "nasa": nasa}
    rows = []
    for name in sys.argv[2:] or sources:
        before = len(rows)
        try:
            sources[name](rows)
        except Exception as e:  # One unavailable source should not lose the others.
            print(f"{name} failed: {type(e).__name__}: {str(e)[:120]}", flush=True)
        print(name, len(rows) - before, flush=True)
    contact_sheets(rows, out)
    (out / "candidates.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print("total", len(rows), "thumb_errors", sum("thumb_error" in r for r in rows))


if __name__ == "__main__":
    main()
