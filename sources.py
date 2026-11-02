"""Bring your own images: a folder on this server, one image address, a JSON list, or a pack.

Images are copied into the library, so the frame keeps working if the folder or
website goes away. Lists from the internet are untrusted: only known fields are
kept, and file names always come from this program.

Packs are lists shipped with this program. Their images are not: they are downloaded
to this server only when someone presses the button on the admin page.
"""
import gzip
import io
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "library" / "manifest.json"
PACKS = HERE / "library" / "packs"
BING_FEED = "https://www.bing.com/HPImageArchive.aspx?format=js&idx=0&n=8&mkt=ko-KR&uhd=1&uhdwidth=3840&uhdheight=2160"
GOOGLE_ARTS_FEED = "https://www.gstatic.com/culturalinstitute/tabext/imax_2_2.json"
TYPES = ("folder", "image", "list")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MIN_SIZE = (1920, 1080)
MAX_WIDTH = 4200
MAX_BYTES = 200 * 1024 * 1024
FETCH_TRIES = 6
WIKIMEDIA_PAUSE_SECONDS = 15
USER_AGENT = {"User-Agent": "TheFrameVanced/0.1"}
LIST_FIELDS = ("caption", "kind", "times", "seasons", "weather", "themes", "fit", "license", "artist", "provider", "source")
Image.MAX_IMAGE_PIXELS = 500_000_000


def slug(text):
    return re.sub(r"[^0-9a-z가-힣]+", "-", str(text).lower()).strip("-")[:60] or "image"


def read_list(path):
    return json.loads(path.read_text()) if path.exists() else []


def write_list(path, value):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    tmp.replace(path)


def load_sources(data_dir):
    return read_list(data_dir / "sources.json")


def add_source(data_dir, name, kind, location, theme):
    name, location, theme = name.strip(), location.strip(), theme.strip()
    if kind not in TYPES:
        raise ValueError("종류를 고르세요")
    if not name or not location:
        raise ValueError("이름과 위치를 입력하세요")
    if kind == "folder":
        if not Path(location).is_dir():
            raise ValueError("서버에서 이 폴더를 찾을 수 없습니다")
    elif not location.startswith(("http://", "https://")):
        raise ValueError("http:// 또는 https:// 로 시작하는 주소를 입력하세요")
    sources = load_sources(data_dir)
    source_id = slug(name)
    if any(s["id"] == source_id for s in sources):
        raise ValueError("같은 이름의 소스가 이미 있습니다")
    source = {"id": source_id, "name": name, "type": kind, "location": location, "theme": theme or name}
    write_list(data_dir / "sources.json", sources + [source])
    return source


def load_packs():
    packs = [json.loads(path.read_text()) for path in sorted(PACKS.glob("*.json"))]
    return sorted(packs, key=lambda pack: pack.get("personal", False))


def add_pack(data_dir, pack_id):
    pack = next((p for p in load_packs() if p["id"] == pack_id), None)
    if pack is None:
        raise ValueError("묶음을 찾을 수 없습니다")
    sources = load_sources(data_dir)
    known = next((s for s in sources if s["id"] == pack_id), None)
    if known:
        return known
    source = {"id": pack_id, "name": pack["name"], "type": "pack", "location": pack_id, "theme": pack["theme"]}
    write_list(data_dir / "sources.json", sources + [source])
    return source


def fetch(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"지원하지 않는 주소입니다: {url[:60]}")
    req = urllib.request.Request(url, headers=USER_AGENT)
    for _ in range(FETCH_TRIES):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read(MAX_BYTES + 1)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 30) + 5)
    else:
        raise ValueError("사이트가 요청을 계속 거절합니다")
    if len(body) > MAX_BYTES:
        raise ValueError("파일이 너무 큽니다")
    # Some sites send the file compressed even though nobody asked for that.
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def store_image(raw, target):
    """Decode, shrink and re-save, so that only real images end up in the library."""
    image = Image.open(io.BytesIO(raw)).convert("RGB")
    if max(image.size) < MIN_SIZE[0] or min(image.size) < MIN_SIZE[1]:
        raise ValueError(f"FHD보다 작습니다: {image.width}x{image.height}")
    if image.width > MAX_WIDTH:
        height = round(image.height * MAX_WIDTH / image.width)
        image = image.resize((MAX_WIDTH, height), Image.LANCZOS)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, quality=92, optimize=True)


def entry(source, name, fields=None):
    item_id = f'{source["id"]}-{slug(name)}'
    base = {
        "id": item_id, "kind": "photo", "caption": str(name), "times": ["any"], "seasons": ["any"], "weather": ["any"],
        "themes": [source["theme"]], "provider": source["name"], "license": "사용자 제공", "artist": "",
    }
    base.update({k: v for k, v in (fields or {}).items() if k in LIST_FIELDS})
    return {**base, "id": item_id, "file": f"images/{item_id}.jpg", "source_id": source["id"]}


def bing_rows():
    rows = []
    for row in json.loads(fetch(BING_FEED))["images"]:
        caption, _, credit = row["copyright"].partition(" (©")
        rows.append({
            "id": row["urlbase"].split(".")[-1].split("_")[0],
            "url": f'https://www.bing.com{row["url"]}',
            "caption": caption,
            "artist": credit.rstrip(")").strip(),
            "source": row.get("copyrightlink", ""),
        })
    return rows


def google_arts_rows():
    return [{
        "id": row["link"].rsplit("/", 1)[-1],
        "url": row["image"].replace("http://", "https://", 1) + "=s3840",
        "caption": f'{row["title"]}, {row["creator"]}',
        "artist": row["creator"],
        "provider": row["attribution"],
        "source": f'https://artsandculture.google.com/{row["link"]}',
    } for row in json.loads(fetch(GOOGLE_ARTS_FEED))]


FEEDS = {"bing": bing_rows, "google-arts": google_arts_rows}


def pack_rows(pack):
    rows = FEEDS[pack["feed"]]() if "feed" in pack else pack["images"]
    return [{**pack.get("defaults", {}), **row} for row in rows]


def items_in(source):
    """Yield (name, fields, load) for every image of a source."""
    if source["type"] == "folder":
        for path in sorted(Path(source["location"]).rglob("*")):
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                yield path.stem, None, path.read_bytes
    elif source["type"] == "image":
        yield source["name"], None, lambda: fetch(source["location"])
    elif source["type"] == "pack":
        pack = next((p for p in load_packs() if p["id"] == source["location"]), None)
        if pack is None:
            raise ValueError("이 버전에 없는 묶음입니다")
        for row in pack_rows(pack):
            yield row["id"], row, lambda url=row["url"]: fetch(url)
    else:
        rows = json.loads(fetch(source["location"]))
        if not isinstance(rows, list):
            raise ValueError("목록은 JSON 배열이어야 합니다")
        for index, row in enumerate(rows, 1):
            if isinstance(row, dict) and row.get("url"):
                name = row.get("id") or row.get("caption") or f"{source['name']} {index}"
                yield name, row, lambda url=row["url"]: fetch(url)


def sync(data_dir, source, lock, on_progress=lambda count: None):
    """Copy new images into the library. Returns (added, failed)."""
    catalog_path = data_dir / "library" / "catalog.json"
    added = failed = 0
    for name, fields, load in items_in(source):
        item = entry(source, name, fields)
        target = data_dir / "library" / item["file"]
        if target.exists():
            continue
        try:
            store_image(load(), target)
        except Exception as e:  # One bad image should not stop the rest.
            print(f'{source["id"]}: skipped {str(name)[:50]}: {type(e).__name__}', flush=True)
            failed += 1
            continue
        with lock:
            catalog = read_list(catalog_path)
            write_list(catalog_path, [i for i in catalog if i["id"] != item["id"]] + [item])
        added += 1
        on_progress(added)
    return added, failed


def base_status(data_dir, manifest=MANIFEST):
    """How far the built-in images on this server are from this version's list."""
    wanted = {e["id"] for e in read_list(manifest)}
    known = {i["id"] for i in read_list(data_dir / "library" / "catalog.json") if not i.get("source_id")}
    return {"total": len(wanted), "new": len(wanted - known), "retired": len(known - wanted)}


def sync_base(data_dir, lock, on_progress=lambda count: None, manifest=MANIFEST):
    """Bring the built-in images in line with this version's list. Returns (added, failed).

    Images that left the list stop being shown. Their files stay on this server.
    """
    catalog_path = data_dir / "library" / "catalog.json"
    entries = read_list(manifest)
    wanted = {e["id"] for e in entries}
    with lock:
        write_list(catalog_path, [i for i in read_list(catalog_path) if i.get("source_id") or i["id"] in wanted])
    added = failed = 0
    for item in entries:
        target = data_dir / "library" / "images" / f'{item["id"]}.jpg'
        fresh = not target.exists()
        if fresh:
            try:
                store_image(fetch(item["url"]), target)
            except Exception as e:  # One bad image should not stop the rest.
                print(f'{item["id"]}: skipped: {type(e).__name__}: {str(e)[:80]}', flush=True)
                failed += 1
                continue
        with lock:
            catalog = read_list(catalog_path)
            old = next((i for i in catalog if i["id"] == item["id"]), {})
            kept = {k: old[k] for k in ("enabled", "focus") if k in old}
            row = {**item, **kept, "file": f'images/{item["id"]}.jpg'}
            write_list(catalog_path, [i for i in catalog if i["id"] != item["id"]] + [row])
        if fresh:
            added += 1
            on_progress(added)
            if "wikimedia.org" in item["url"]:
                time.sleep(WIKIMEDIA_PAUSE_SECONDS)
    return added, failed


def remove_source(data_dir, source_id, lock):
    """Forget a source and the copies made from it. The original folder or website is not touched."""
    with lock:
        sources = load_sources(data_dir)
        if not any(s["id"] == source_id for s in sources):
            raise ValueError("소스를 찾을 수 없습니다")
        catalog_path = data_dir / "library" / "catalog.json"
        catalog = read_list(catalog_path)
        for item in catalog:
            if item.get("source_id") == source_id:
                (data_dir / "library" / item["file"]).unlink(missing_ok=True)
        write_list(catalog_path, [i for i in catalog if i.get("source_id") != source_id])
        write_list(data_dir / "sources.json", [s for s in sources if s["id"] != source_id])
