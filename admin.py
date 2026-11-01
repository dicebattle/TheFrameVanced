#!/usr/bin/env python3
"""Local admin page for TheFrameVanced. Binds to localhost only: there is no login."""
import argparse
import base64
import concurrent.futures as cf
import io
import json
import os
import socket
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from PIL import Image

import frame
import sources

HERE = Path(__file__).resolve().parent
SERVICE = "theframevanced"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", *filter(None, os.environ.get("ADMIN_HOSTS", "").split(","))}
PREVIEW_SIZE = (1280, 720)
THUMB_SIZE = (360, 203)
USER_AGENT = {"User-Agent": "TheFrameVanced/0.1"}

app = Flask(__name__, static_folder=None)
data_dir = None
write_lock = threading.Lock()
source_status = {}


def read_json(name, default):
    return frame.load_json(data_dir / name, default)


def write_json(name, value):
    path = data_dir / name
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    tmp.replace(path)


def catalog():
    return read_json("library/catalog.json", [])


def find_item(item_id):
    item = next((i for i in catalog() if i["id"] == item_id), None)
    if item is None:
        abort(404, "이미지를 찾을 수 없습니다")
    return item


def systemctl(*args):
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)


@app.before_request
def guard():
    # Blocks other websites from reaching this page through the visitor's browser.
    if request.host.split(":")[0] not in ALLOWED_HOSTS:
        abort(403)
    if request.method == "POST" and not request.is_json:
        abort(415)


@app.get("/")
def index():
    return send_from_directory(HERE / "admin_ui", "index.html")


@app.get("/api/state")
def state():
    timer = systemctl("is-enabled", f"{SERVICE}.timer").stdout.strip()
    cfg = read_json("config.json", {})
    return jsonify(
        config=cfg,
        layout={**frame.DEFAULT_LAYOUT, **cfg.get("layout", {})},
        state=read_json("state.json", {}),
        cleanup=(data_dir / "cleanup.enabled").exists(),
        timer=timer == "enabled",
        catalog=catalog(),
    )


@app.post("/api/config")
def save_config():
    body = request.get_json()
    with write_lock:
        cfg = read_json("config.json", {})
        layout = body.pop("layout", None)
        allowed = {"tv_host", "tv_inches", "change_minutes", "min_weather_change_minutes", "disabled_themes"}
        if body.get("tv_host") not in (None, cfg.get("tv_host")):
            # A different TV may have a different size: read it again from its model name.
            cfg.pop("tv_inches", None)
        cfg.update({k: v for k, v in body.items() if k in allowed})
        if layout is not None:
            known = {k: v for k, v in layout.items() if k in frame.DEFAULT_LAYOUT}
            cfg["layout"] = {**cfg.get("layout", {}), **known}
        write_json("config.json", cfg)
    return jsonify(ok=True)


def get_json(url, params):
    req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


@app.post("/api/geocode")
def geocode():
    """Search OpenStreetMap. Runs only when someone presses the search button."""
    query = request.get_json()["query"].strip()
    if not query:
        return jsonify([])
    rows = get_json(
        "https://nominatim.openstreetmap.org/search",
        {"q": query, "format": "jsonv2", "limit": 5, "accept-language": "ko"},
    )
    return jsonify([{"name": r["display_name"], "latitude": float(r["lat"]), "longitude": float(r["lon"])} for r in rows])


@app.post("/api/location")
def save_location():
    body = request.get_json()
    latitude, longitude = round(float(body["latitude"]), 4), round(float(body["longitude"]), 4)
    # Open-Meteo works out the time zone from the coordinates.
    tz = get_json(
        "https://api.open-meteo.com/v1/forecast",
        {"latitude": latitude, "longitude": longitude, "timezone": "auto", "forecast_days": 1, "daily": "sunrise"},
    )["timezone"]
    with write_lock:
        cfg = read_json("config.json", {})
        cfg.update(
            location_label=body["query"].strip(),
            location_detail=body["name"],
            latitude=latitude,
            longitude=longitude,
            timezone=tz,
        )
        write_json("config.json", cfg)
    return jsonify(ok=True, timezone=tz)


@app.get("/api/context")
def context():
    cfg = read_json("config.json", {})
    return jsonify(frame.build_context(cfg, datetime.now(ZoneInfo(cfg["timezone"]))))


@app.post("/api/preview")
def preview():
    body = request.get_json()
    cfg = read_json("config.json", {})
    cfg["layout"] = {**frame.DEFAULT_LAYOUT, **cfg.get("layout", {}), **body.get("layout", {})}
    layout = frame.effective_layout(cfg)
    item = find_item(body["image"])
    ctx = body["context"]
    image = frame.compose(item, ctx, cfg, data_dir).resize(PREVIEW_SIZE, Image.LANCZOS)
    out = io.BytesIO()
    image.save(out, "JPEG", quality=82)
    rows = frame.caption_rows(frame.caption_lines(item, ctx, cfg, layout), layout)
    box = frame.caption_box(rows, layout)[0] if rows else None
    return jsonify(image=base64.b64encode(out.getvalue()).decode(), box=box, canvas=frame.CANVAS)


@app.get("/api/thumb/<item_id>")
def thumb(item_id):
    item = find_item(item_id)
    cache = data_dir / "output" / "thumbs"
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{item_id}.jpg"
    if not path.exists():
        with Image.open(data_dir / "library" / item["file"]) as src:
            image = src.convert("RGB")
            image.thumbnail(THUMB_SIZE)
            image.save(path, quality=80)
    return Response(path.read_bytes(), mimetype="image/jpeg", headers={"Cache-Control": "max-age=86400"})


@app.post("/api/library/<item_id>")
def update_item(item_id):
    body = request.get_json()
    allowed = {"enabled", "times", "seasons", "weather", "caption", "focus", "themes"}
    with write_lock:
        items = catalog()
        for item in items:
            if item["id"] == item_id:
                item.update({k: v for k, v in body.items() if k in allowed})
                write_json("library/catalog.json", items)
                return jsonify(ok=True)
    abort(404)


def start_sync(source):
    status = source_status[source["id"]] = {"running": True, "added": 0, "failed": 0, "error": None}

    def run():
        try:
            status["added"], status["failed"] = sources.sync(
                data_dir, source, write_lock, on_progress=lambda n: status.update(added=n)
            )
        except Exception as e:
            status["error"] = str(e)[:200]
        status["running"] = False

    threading.Thread(target=run, daemon=True).start()


@app.get("/api/sources")
def list_sources():
    counts = {}
    for item in catalog():
        counts[item.get("source_id")] = counts.get(item.get("source_id"), 0) + 1
    rows = [{**s, "images": counts.get(s["id"], 0), "status": source_status.get(s["id"])} for s in sources.load_sources(data_dir)]
    return jsonify(rows)


@app.post("/api/sources")
def create_source():
    body = request.get_json()
    try:
        with write_lock:
            source = sources.add_source(data_dir, body.get("name", ""), body.get("type", ""), body.get("location", ""), body.get("theme", ""))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    start_sync(source)
    return jsonify(ok=True, source=source)


@app.post("/api/sources/<source_id>/sync")
def resync_source(source_id):
    source = next((s for s in sources.load_sources(data_dir) if s["id"] == source_id), None)
    if source is None:
        abort(404)
    start_sync(source)
    return jsonify(ok=True)


@app.post("/api/sources/<source_id>/remove")
def delete_source(source_id):
    try:
        sources.remove_source(data_dir, source_id, write_lock)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 404
    source_status.pop(source_id, None)
    return jsonify(ok=True)


def rest_info(host, timeout=3):
    try:
        with urllib.request.urlopen(f"http://{host}:8001/api/v2/", timeout=timeout) as r:
            device = json.load(r).get("device", {})
    except (OSError, ValueError):
        return None
    return {
        "host": host,
        "model": device.get("modelName"),
        "name": device.get("name"),
        "frame": device.get("FrameTVSupport") == "true",
        "power": device.get("PowerState"),
        "network": device.get("networkType"),
    }


def art_info(host):
    from samsungtvws import SamsungTVWS

    result = {}

    def query():
        tv = SamsungTVWS(host, port=8002, name="TheFrameVanced", timeout=6)
        art = tv.art()
        try:
            result["art_mode"] = art.get_artmode()
            result["current"] = art.get_current().get("content_id")
            result["api_version"] = art.get_api_version()
            result["uploaded"] = len(art.available(category="MY-C0002"))
        finally:
            tv.close()

    worker = threading.Thread(target=query, daemon=True)
    worker.start()
    worker.join(10)
    return result


@app.get("/api/tv")
def tv_status():
    cfg = read_json("config.json", {})
    host = cfg.get("tv_host")
    if not host:
        return jsonify(reachable=False, host=None, standby=False)
    info = rest_info(host)
    if not info:
        return jsonify(reachable=False, host=host, standby=frame.ping_port(host, 7678))
    try:
        info.update(art_info(host))
    except Exception as e:
        info["art_error"] = str(e)[:160]
    inches = frame.inches_from_model(info["model"])
    if inches and "tv_inches" not in cfg:
        with write_lock:
            fresh = read_json("config.json", {})
            fresh["tv_inches"] = cfg["tv_inches"] = inches
            write_json("config.json", fresh)
    return jsonify(reachable=True, tv_inches=cfg.get("tv_inches"), **info)


@app.post("/api/tv/discover")
def discover():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect(("10.255.255.255", 1))
    prefix = probe.getsockname()[0].rsplit(".", 1)[0]
    probe.close()
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    with cf.ThreadPoolExecutor(64) as pool:
        open_hosts = [h for h, ok in zip(hosts, pool.map(lambda h: frame.ping_port(h, 8001), hosts)) if ok]
        found = [i for i in pool.map(rest_info, open_hosts) if i]
    return jsonify(found)


@app.post("/api/apply")
def apply_now():
    body = request.get_json()
    cmd = [sys.executable, "-W", "ignore", str(HERE / "frame.py"), "--data", str(data_dir), "--force"]
    if body.get("image"):
        cmd += ["--image", body["image"]]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=170)
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, output="170초 안에 끝나지 않아 중단했습니다"), 504
    return jsonify(ok=run.returncode == 0, output=(run.stdout + run.stderr)[-1500:])


@app.post("/api/timer")
def set_timer():
    action = "enable" if request.get_json()["enabled"] else "disable"
    run = systemctl(action, "--now", f"{SERVICE}.timer")
    return jsonify(ok=run.returncode == 0, output=run.stderr[-300:])


@app.post("/api/cleanup")
def set_cleanup():
    flag = data_dir / "cleanup.enabled"
    if request.get_json()["enabled"]:
        flag.touch()
    else:
        flag.unlink(missing_ok=True)
    return jsonify(ok=True, cleanup=flag.exists())


@app.get("/api/log")
def log():
    run = subprocess.run(
        ["journalctl", "--user", "-u", SERVICE, "-n", "40", "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True, timeout=15,
    )
    return jsonify(lines=run.stdout.splitlines()[-40:])


def main():
    global data_dir
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    data_dir = args.data.resolve()
    app.run(host="127.0.0.1", port=args.port, threaded=True)


if __name__ == "__main__":
    main()
