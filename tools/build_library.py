#!/usr/bin/env python3
"""Download the images listed in library/manifest.json and write catalog.json.

Usage: python tools/build_library.py library/manifest.json data/library
Safe to run again: images that already exist are skipped.
"""
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

MAX_WIDTH = 4200
UA = "TheFrameVanced/0.1"
PAUSE_SECONDS = 15
Image.MAX_IMAGE_PIXELS = None


def fetch(url):
    for _ in range(8):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            wait = int(e.headers.get("Retry-After") or 30)
            print(f"rate limited, waiting {wait}s", flush=True)
            time.sleep(wait + 5)
    raise RuntimeError("still rate limited")


def main():
    manifest, library = Path(sys.argv[1]), Path(sys.argv[2])
    images = library / "images"
    images.mkdir(parents=True, exist_ok=True)
    catalog_path = library / "catalog.json"
    catalog = {}
    if catalog_path.exists():
        catalog = {i["id"]: i for i in json.loads(catalog_path.read_text())}

    for entry in json.loads(manifest.read_text()):
        slug = entry["id"]
        target = images / f"{slug}.jpg"
        if not target.exists():
            image = Image.open(io.BytesIO(fetch(entry["url"]))).convert("RGB")
            if image.width > MAX_WIDTH:
                height = round(image.height * MAX_WIDTH / image.width)
                image = image.resize((MAX_WIDTH, height), Image.LANCZOS)
            image.save(target, quality=92, optimize=True)
            print(f"saved {slug} {image.size}", flush=True)
            # Wikimedia limits downloads; the museum and NASA servers do not need this wait.
            if "wikimedia.org" in entry["url"]:
                time.sleep(PAUSE_SECONDS)
        kept = {"enabled": catalog[slug]["enabled"]} if "enabled" in catalog.get(slug, {}) else {}
        catalog[slug] = {**entry, **kept, "file": f"images/{slug}.jpg"}
        catalog_path.write_text(json.dumps(list(catalog.values()), ensure_ascii=False, indent=1))
    print("catalog size", len(catalog))


if __name__ == "__main__":
    main()
