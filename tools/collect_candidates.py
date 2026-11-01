"""Collect wide, high-resolution candidates from Wikimedia Commons and build numbered contact sheets."""
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

OUT = Path(sys.argv[1])
UA = "TheFrameVanced/0.1"
API = "https://commons.wikimedia.org/w/api.php"
SIZE = "filew:>3800 fileh:>1900"
GROUPS = {
    "landscape": [f'incategory:"Featured pictures of landscapes" {SIZE}'],
    "city": [
        f'incategory:"Featured pictures of cityscapes" {SIZE}',
        f'incategory:"Featured pictures of skylines" {SIZE}',
    ],
    "aerial": [f'incategory:"Featured pictures from unmanned aerial vehicles" {SIZE}'],
    "golden": [
        f'incategory:"Featured pictures of {c}" {SIZE}'
        for c in ("twilights", "sunsets", "sunrises", "aurora")
    ],
    "korea": [
        f"{q} filetype:bitmap {SIZE}"
        for q in (
            "Seoul skyline night",
            "Lotte World Tower Seoul aerial",
            "Busan Gwangan bridge night",
            "Seoraksan autumn",
            "Jeju Hallasan",
        )
    ],
}
PAINTINGS = [
    "Starry Night Google Art Project",
    "Monet Water Lilies",
    "Monet Impression Sunrise",
    "Monet Haystacks",
    "Monet Poplars",
    "Van Gogh Wheat Field with Cypresses",
    "Van Gogh Almond Blossom",
    "Van Gogh Starry Night Over the Rhone",
    "Van Gogh Cafe Terrace",
    "Van Gogh Harvest",
    "Hokusai Great Wave",
    "Hokusai Fine Wind Clear Morning",
    "Hiroshige Sudden Shower",
    "Turner Fighting Temeraire",
    "Turner Rain Steam and Speed",
    "Caspar David Friedrich Sea of Ice",
    "Caspar David Friedrich Moonrise",
    "Bierstadt Among the Sierra Nevada",
    "Frederic Edwin Church Heart of the Andes",
    "Church Twilight in the Wilderness",
    "Thomas Cole Oxbow",
    "Hopper Nighthawks",
    "Renoir Luncheon Boating Party",
    "Seurat Sunday La Grande Jatte",
    "Pissarro Boulevard Montmartre",
    "Sisley Snow Louveciennes",
    "Klimt Beech Grove",
    "Aivazovsky Ninth Wave",
    "Constable Hay Wain",
    "Canaletto Venice Grand Canal",
]


def fetch(url):
    for attempt in range(6):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            wait = int(e.headers.get("Retry-After") or 30)
            print(f"429, waiting {wait}s", flush=True)
            time.sleep(min(wait, 120) + attempt * 5)
    raise RuntimeError("still rate limited")


def api(**params):
    params.update(action="query", format="json")
    return json.loads(fetch(API + "?" + urllib.parse.urlencode(params)))


def strip(value):
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def search(query, limit):
    data = api(
        generator="search",
        gsrnamespace=6,
        gsrsearch=query,
        gsrlimit=limit,
        prop="imageinfo",
        iiprop="url|size|mime|extmetadata",
        iiurlwidth=500,
        iiextmetadatafilter="LicenseShortName|Artist|ObjectName|DateTimeOriginal",
    )
    return list(data.get("query", {}).get("pages", {}).values())


def collect(group, query, limit, seen, rows, lo=1.45, hi=2.1, minw=3800):
    for page in search(query, limit):
        info = (page.get("imageinfo") or [{}])[0]
        w, h = info.get("width", 0), info.get("height", 0)
        if page["title"] in seen or info.get("mime") != "image/jpeg":
            continue
        if not h or w < minw or not lo <= w / h <= hi:
            continue
        seen.add(page["title"])
        meta = info.get("extmetadata", {})
        rows.append(
            {
                "group": group,
                "title": page["title"],
                "url": info["url"],
                "thumb": info["thumburl"],
                "w": w,
                "h": h,
                "license": strip(meta.get("LicenseShortName", {}).get("value")),
                "artist": strip(meta.get("Artist", {}).get("value"))[:80],
                "name": strip(meta.get("ObjectName", {}).get("value"))[:100],
            }
        )


def main():
    rows, seen = [], set()
    for group, queries in GROUPS.items():
        for q in queries:
            collect(group, q, 40, seen, rows)
            time.sleep(2)
    for q in PAINTINGS:
        query = q + " filetype:bitmap filew:>2800"
        collect("painting", query, 8, seen, rows, lo=1.2, hi=2.3, minw=2800)
        time.sleep(1.5)
    print({g: sum(r["group"] == g for r in rows) for g in list(GROUPS) + ["painting"]}, flush=True)

    font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 30)
    cols, nrows, tw, th = 5, 6, 372, 209
    per_group = {}
    for r in rows:
        per_group.setdefault(r["group"], []).append(r)
    index = 0
    for group, items in per_group.items():
        for s in range(0, len(items), cols * nrows):
            sheet = Image.new("RGB", (cols * tw, nrows * th), "black")
            d = ImageDraw.Draw(sheet)
            for i, r in enumerate(items[s : s + cols * nrows]):
                index += 1
                r["id"] = index
                try:
                    im = Image.open(io.BytesIO(fetch(r["thumb"]))).convert("RGB")
                    im.thumbnail((tw - 4, th - 4))
                    x, y = (i % cols) * tw + 2, (i // cols) * th + 2
                    sheet.paste(im, (x, y))
                    d.rectangle((x, y, x + 76, y + 38), fill="black")
                    d.text((x + 6, y - 3), str(r["id"]), fill="yellow", font=font)
                except Exception as e:
                    r["thumb_error"] = str(e)[:80]
                time.sleep(1.2)
            sheet.save(OUT / f"sheet_{group}_{s // (cols * nrows) + 1}.jpg", quality=82)
    (OUT / "candidates.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print("total", len(rows), "thumb_errors", sum("thumb_error" in r for r in rows))


main()
