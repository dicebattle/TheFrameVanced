"""Bring your own images: a folder on this server, one image address, or a JSON list.

Images are copied into the library, so the frame keeps working if the folder or
website goes away. Lists from the internet are untrusted: only known fields are
kept, and file names always come from this program.
"""
import io
import json
import re
import urllib.request
from pathlib import Path

from PIL import Image

TYPES = ("folder", "image", "list")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_WIDTH = 4200
MAX_BYTES = 80 * 1024 * 1024
USER_AGENT = {"User-Agent": "TheFrameVanced/0.1"}
LIST_FIELDS = ("caption", "kind", "times", "seasons", "weather", "themes", "fit", "license", "artist")


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


def fetch(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"지원하지 않는 주소입니다: {url[:60]}")
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("파일이 너무 큽니다")
    return body


def store_image(raw, target):
    """Decode, shrink and re-save, so that only real images end up in the library."""
    image = Image.open(io.BytesIO(raw)).convert("RGB")
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


def items_in(source):
    """Yield (name, fields, load) for every image of a source."""
    if source["type"] == "folder":
        for path in sorted(Path(source["location"]).rglob("*")):
            if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file():
                yield path.stem, None, path.read_bytes
    elif source["type"] == "image":
        yield source["name"], None, lambda: fetch(source["location"])
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
