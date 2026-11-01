#!/usr/bin/env python3
"""TheFrameVanced: turn a Samsung The Frame TV into a living digital frame.

Picks an image that matches the local time of day, weather and season, prints a
small caption on it and shows it in Art Mode.
"""
import argparse
import json
import random
import re
import signal
import socket
import ssl
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import ephem
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

CANVAS = (3840, 2160)
HISTORY_SIZE = 40
RUN_TIMEOUT_SECONDS = 150
FONT_PATHS = {
    "Regular": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "Bold": "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
}
FONT_INDEX_KR = 1
# Text sizes are stored for a 75 inch TV and scaled so the printed size stays the same on any TV.
REFERENCE_INCHES = 75
# Small TVs are watched from closer, so the size is not matched all the way down.
MAX_TEXT_SCALE = 1.8
# Keeps text inside the screen edge, whatever its size.
EDGE_MARGIN = 96
# Relative luminance (0-1) from which dark text reads better than light text.
BRIGHT_BACKGROUND = 0.30
LINEAR_LUT = [
    round(255 * (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4))
    for c in (i / 255 for i in range(256))
] * 3
TEXT_TONES = {
    "light": {"color": "#F6F4EE", "caption_color": "#ECEAE4", "veil": (12, 14, 18), "shadow": (0, 0, 0)},
    "dark": {"color": "#14171A", "caption_color": "#2B2F34", "veil": (248, 247, 243), "shadow": (255, 255, 255)},
}
DEFAULT_LAYOUT = {
    "x": 0.031,
    "y": 0.865,
    "align": "left",
    "headline": "{weather} {temp}°",
    "caption": "{title}",
    "headline_size": 92,
    "caption_size": 56,
    "weight": "Regular",
    "line_gap": 34,
    "color_mode": "auto",
    "color": "#F6F4EE",
    "caption_color": "#ECEAE4",
    "veil": 1.0,
    "shadow": 190,
}

# Ordered by solar longitude: index 0 is the spring equinox (0 degrees).
SOLAR_TERMS = [
    "춘분", "청명", "곡우", "입하", "소만", "망종", "하지", "소서", "대서", "입추", "처서", "백로",
    "추분", "한로", "상강", "입동", "소설", "대설", "동지", "소한", "대한", "입춘", "우수", "경칩",
]
WEATHER_LABELS = {
    0: "맑음", 1: "대체로 맑음", 2: "구름 조금", 3: "흐림", 45: "안개", 48: "안개",
    51: "이슬비", 53: "이슬비", 55: "이슬비", 56: "어는 이슬비", 57: "어는 이슬비",
    61: "비", 63: "비", 65: "강한 비", 66: "어는 비", 67: "어는 비",
    71: "눈", 73: "눈", 75: "강한 눈", 77: "싸락눈",
    80: "소나기", 81: "소나기", 82: "강한 소나기", 85: "소낙눈", 86: "소낙눈",
    95: "뇌우", 96: "뇌우", 99: "뇌우",
}


def weather_class(code):
    if code in (0, 1):
        return "clear"
    if code in (2, 3):
        return "cloud"
    if code in (45, 48):
        return "fog"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    return "rain"


def solar_term(now):
    """Return the current solar term name and its season."""
    date = ephem.Date(now.astimezone(ZoneInfo("UTC")).replace(tzinfo=None))
    sun = ephem.Sun(date)
    longitude = float(ephem.Ecliptic(sun, epoch=date).lon) * 180 / ephem.pi
    index = int(longitude // 15) % 24
    if index >= 21 or index <= 2:
        season = "spring"
    elif index <= 8:
        season = "summer"
    elif index <= 14:
        season = "autumn"
    else:
        season = "winter"
    return SOLAR_TERMS[index], season


def time_band(now, sunrise, sunset):
    if sunrise - timedelta(minutes=50) <= now < sunrise + timedelta(minutes=45):
        return "dawn"
    if sunset - timedelta(minutes=80) <= now < sunset + timedelta(minutes=35):
        return "sunset"
    if now < sunrise or now >= sunset:
        return "night"
    return "morning" if now.hour < 11 else "day"


def fetch_weather(cfg):
    query = urllib.parse.urlencode(
        {
            "latitude": cfg["latitude"],
            "longitude": cfg["longitude"],
            "current": "temperature_2m,weather_code",
            "daily": "sunrise,sunset",
            "forecast_days": 1,
            "timezone": cfg["timezone"],
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{query}"
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def build_context(cfg, now):
    tz = ZoneInfo(cfg["timezone"])
    data = fetch_weather(cfg)
    code = int(data["current"]["weather_code"])
    sunrise = datetime.fromisoformat(data["daily"]["sunrise"][0]).replace(tzinfo=tz)
    sunset = datetime.fromisoformat(data["daily"]["sunset"][0]).replace(tzinfo=tz)
    term, season = solar_term(now)
    return {
        "band": time_band(now, sunrise, sunset),
        "weather": weather_class(code),
        "weather_label": WEATHER_LABELS.get(code, "날씨 정보 없음"),
        "temperature": round(data["current"]["temperature_2m"]),
        "term": term,
        "season": season,
    }


def fits(item, key, value):
    allowed = item.get(key) or ["any"]
    return "any" in allowed or value in allowed


def score(item, ctx, last_kind):
    if not fits(item, "seasons", ctx["season"]):
        return None
    is_night = ctx["band"] == "night"
    times = item.get("times") or ["any"]
    if "any" not in times and is_night != ("night" in times) and ctx["band"] not in times:
        return None
    total = 0
    total += 4 if ctx["band"] in times else (1 if "any" in times else 0)
    total += 2 if ctx["season"] in (item.get("seasons") or []) else 1
    weather = item.get("weather") or ["any"]
    total += 3 if ctx["weather"] in weather else (1 if "any" in weather else 0)
    total += 2 if item.get("kind") != last_kind else 0
    return total + random.uniform(0, 1.5)


def in_chosen_themes(item, disabled):
    themes = item.get("themes") or []
    return not themes or any(t not in disabled for t in themes)


def pick_image(catalog, ctx, state, disabled_themes=()):
    catalog = [i for i in catalog if i.get("enabled", True) and in_chosen_themes(i, disabled_themes)]
    if not catalog:
        raise SystemExit("no images left: turn on a theme or an image in the admin page")
    recent = set(state.get("history", []))
    scored = [(score(i, ctx, state.get("kind")), i) for i in catalog if i["id"] not in recent]
    scored = [(s, i) for s, i in scored if s is not None]
    if not scored:
        scored = [(score(i, ctx, None) or 0, i) for i in catalog]
    return max(scored, key=lambda pair: pair[0])[1]


def cover_crop(image, focus):
    width, height = image.size
    ratio = CANVAS[0] / CANVAS[1]
    if width / height > ratio:
        new_width = int(height * ratio)
        left = int((width - new_width) * focus[0])
        box = (left, 0, left + new_width, height)
    else:
        new_height = int(width / ratio)
        top = int((height - new_height) * focus[1])
        box = (0, top, width, top + new_height)
    return image.crop(box).resize(CANVAS, Image.LANCZOS)


def hex_color(value):
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def caption_lines(item, ctx, cfg, layout):
    values = {
        "location": cfg["location_label"],
        "term": ctx["term"],
        "weather": ctx["weather_label"],
        "temp": ctx["temperature"],
        "title": item.get("caption", ""),
    }
    fill = lambda text: re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), text)
    return [fill(layout["headline"]), fill(layout["caption"])]


def caption_rows(lines, layout):
    path = FONT_PATHS[layout["weight"]]
    big = ImageFont.truetype(path, layout["headline_size"], index=FONT_INDEX_KR)
    small = ImageFont.truetype(path, layout["caption_size"], index=FONT_INDEX_KR)
    pairs = ((lines[0], big, "color"), (lines[1], small, "caption_color"))
    return [(t, f, role, f.size) for t, f, role in pairs if t.strip()]


def caption_box(rows, layout):
    """Pixel box of the text block. x and y are the 0-1 position of its anchor."""
    block_h = sum(r[3] for r in rows) + layout["line_gap"] * (len(rows) - 1)
    block_w = max(int(r[1].getlength(r[0])) for r in rows)
    origin = {"left": 0.0, "center": 0.5, "right": 1.0}[layout["align"]]
    left = int(layout["x"] * CANVAS[0] - block_w * origin)
    left = max(EDGE_MARGIN, min(left, CANVAS[0] - block_w - EDGE_MARGIN))
    top = int(layout["y"] * CANVAS[1])
    top = max(EDGE_MARGIN, min(top, CANVAS[1] - block_h - EDGE_MARGIN))
    return (left, top, left + block_w, top + block_h), origin


def relative_luminance(image, box):
    """Perceived brightness of an area, 0 for black and 1 for white."""
    r, g, b = ImageStat.Stat(image.crop(box).resize((96, 24)).point(LINEAR_LUT)).mean
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def veil_strength(image, box, dark_text):
    """How strongly to soften the area behind the text, 0-255 before the user's setting."""
    gray = image.crop(box).convert("L")
    if dark_text:
        return min(170, max(0, 235 - ImageStat.Stat(gray).mean[0]) * 1.2)
    # Scattered lights hardly move the average but still fight with light text, so use the
    # brightest few percent. Plain dark walls stay below the limit and get no visible edge.
    histogram = gray.histogram()
    remaining = gray.width * gray.height * 0.03
    for level in range(255, -1, -1):
        remaining -= histogram[level]
        if remaining <= 0:
            break
    return min(190, max(0, level - 70) * 1.3)


def text_style(image, box, layout):
    if layout["color_mode"] == "auto":
        bright = relative_luminance(image, box) >= BRIGHT_BACKGROUND
        return TEXT_TONES["dark" if bright else "light"]
    return {**TEXT_TONES["light"], "color": layout["color"], "caption_color": layout["caption_color"]}


def draw_caption(image, lines, layout):
    rows = caption_rows(lines, layout)
    if not rows:
        return image
    box, origin = caption_box(rows, layout)
    left, top, right, _ = box
    block_w = right - left
    style = text_style(image, box, layout)
    dark_text = style is TEXT_TONES["dark"]

    if layout["veil"] > 0:
        # A soft veil keeps the text readable without looking like a label.
        strength = min(190, veil_strength(image, box, dark_text) * layout["veil"])
        veil = Image.new("L", CANVAS, 0)
        veil_box = (box[0] - 90, box[1] - 70, box[2] + 90, box[3] + 60)
        ImageDraw.Draw(veil).rectangle(veil_box, fill=int(strength))
        veil = veil.filter(ImageFilter.GaussianBlur(70))
        image = Image.composite(Image.new("RGB", CANVAS, style["veil"]), image, veil)

    positions = []
    y = top
    for text, font, role, size in rows:
        x = left + int((block_w - font.getlength(text)) * origin)
        positions.append((x, y, text, font, style[role]))
        y += size + layout["line_gap"]

    if layout["shadow"] > 0:
        shadow = Image.new("L", CANVAS, 0)
        d = ImageDraw.Draw(shadow)
        fill = layout["shadow"] // 2 if dark_text else layout["shadow"]
        for x, y, text, font, _ in positions:
            d.text((x, y), text, fill=fill, font=font)
        shadow = shadow.filter(ImageFilter.GaussianBlur(6))
        image = Image.composite(Image.new("RGB", CANVAS, style["shadow"]), image, shadow)

    d = ImageDraw.Draw(image)
    for x, y, text, font, color in positions:
        d.text((x, y), text, fill=hex_color(color), font=font)
    return image


def hang_on_wall(image):
    """Keep a painting's proportions: show it uncropped on a dark wall tinted by its colors."""
    max_w, max_h, top = 3360, 1720, 90
    scale = min(max_w / image.width, max_h / image.height)
    art = image.resize((round(image.width * scale), round(image.height * scale)), Image.LANCZOS)
    mean = ImageStat.Stat(image.resize((64, 64))).mean
    wall = Image.new("RGB", CANVAS, tuple(int(c * 0.16 + 14) for c in mean))
    left = (CANVAS[0] - art.width) // 2
    shadow = Image.new("L", CANVAS, 0)
    ImageDraw.Draw(shadow).rectangle((left, top + 14, left + art.width, top + art.height + 14), fill=170)
    shadow = shadow.filter(ImageFilter.GaussianBlur(28))
    wall = Image.composite(Image.new("RGB", CANVAS, (0, 0, 0)), wall, shadow)
    wall.paste(art, (left, top))
    return wall


def inches_from_model(model):
    """Samsung model names carry the size after the region letters, as in QE55LS03."""
    match = re.match(r"[A-Z]{2}(\d{2})", model or "")
    inches = int(match[1]) if match else None
    return inches if inches and inches >= 32 else None


def effective_layout(cfg):
    layout = {**DEFAULT_LAYOUT, **cfg.get("layout", {})}
    scale = min(MAX_TEXT_SCALE, REFERENCE_INCHES / (cfg.get("tv_inches") or REFERENCE_INCHES))
    for key in ("headline_size", "caption_size", "line_gap"):
        layout[key] = round(layout[key] * scale)
    return layout


def compose(item, ctx, cfg, data_dir):
    layout = effective_layout(cfg)
    with Image.open(data_dir / "library" / item["file"]) as src:
        image = src.convert("RGB")
    if item.get("fit") == "mat":
        image = hang_on_wall(image)
    else:
        image = cover_crop(image, item.get("focus", [0.5, 0.5]))
    return draw_caption(image, caption_lines(item, ctx, cfg, layout), layout)


def output_size(resolution):
    """The TV's own resolution when it is lower than the 4K canvas."""
    match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", resolution or "")
    size = (int(match[1]), int(match[2])) if match else CANVAS
    return size if size[0] < CANVAS[0] else CANVAS


def render(item, ctx, cfg, data_dir, out_path, size=CANVAS):
    image = compose(item, ctx, cfg, data_dir)
    if size != CANVAS:
        image = image.resize(size, Image.LANCZOS)
    image.save(out_path, quality=90, optimize=True)


def ping_port(host, port, timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tv_reachable(host):
    return ping_port(host, 8002, timeout=3)


def tv_info(host, timeout=3):
    """Read the TV's device card. Needs no pairing and shows nothing on the TV."""
    insecure = ssl.create_default_context()
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE  # TVs use a self-signed certificate.
    for url, context in ((f"http://{host}:8001/api/v2/", None), (f"https://{host}:8002/api/v2/", insecure)):
        try:
            with urllib.request.urlopen(url, timeout=timeout, context=context) as r:
                device = json.load(r).get("device", {})
        except (OSError, ValueError):
            continue
        return {
            "host": host,
            "model": device.get("modelName"),
            "name": device.get("name"),
            "frame": str(device.get("FrameTVSupport")).lower() == "true",
            "power": device.get("PowerState"),
            "network": device.get("networkType"),
            "resolution": device.get("resolution"),
        }
    return None


def record(profile, feature, ok, detail=""):
    """Remember what worked on this TV. ok is True, False, or None when it could not be told."""
    profile.setdefault("features", {})[feature] = {
        "ok": ok,
        "detail": str(detail)[:160],
        "at": datetime.now().isoformat(timespec="minutes"),
    }


def feature_failed(profile, feature):
    return profile.get("features", {}).get(feature, {}).get("ok") is False


def my_images(art):
    """Images uploaded to the TV. The folder id is not the same on every model."""
    try:
        rows = art.available(category="MY-C0002")
    except Exception:
        rows = art.available()
    return {str(i.get("content_id")) for i in rows if str(i.get("content_id", "")).startswith("MY_")}


def check_tv(art, profile):
    """Read-only checks, one at a time, so that one unsupported call does not hide the rest."""
    found = {}
    for key, feature, read in (
        ("art_mode", "read_status", art.get_artmode),
        ("current", None, lambda: art.get_current().get("content_id")),
        ("api_version", None, art.get_api_version),
        ("uploaded", "list_images", lambda: len(my_images(art))),
    ):
        try:
            found[key] = read()
        except Exception as e:
            found[key] = None
            if feature:
                record(profile, feature, False, f"{type(e).__name__}: {e}")
        else:
            if feature:
                record(profile, feature, True)
    return found


def update_tv(art, path, state, cleanup, profile):
    art_on = art.get_artmode() == "on"
    record(profile, "read_status", True)
    before = my_images(art)
    uploaded = state.setdefault("uploaded", [])
    try:
        new_id = art.upload(path.read_bytes(), file_type="JPEG", matte="none", portrait_matte="none")
    except Exception as e:
        # Some models store the image but never send the confirmation.
        found = my_images(art) - before
        if len(found) != 1:
            record(profile, "upload", False, f"{type(e).__name__}: {e}")
            raise
        new_id = found.pop()
    record(profile, "upload", True)
    uploaded.append(new_id)

    # Never call set_artmode here: on some models it does not reply and blocks forever.
    art.select_image(new_id, show=art_on)
    current = art.get_current().get("content_id")
    shown = current == new_id
    if shown:
        record(profile, "show", True)
    elif art_on:
        record(profile, "show", False, f"TV shows {current}, expected {new_id}")
    else:
        # Outside Art Mode some models only report the new image once they return to it.
        record(profile, "show", None, "could not be confirmed outside Art Mode")
    print(f"shown={shown} {new_id} art_mode={art_on}", flush=True)

    # Only ever remove images this program uploaded, and only once the new one is confirmed.
    stale = [i for i in uploaded if i != new_id and i.startswith("MY_")]
    if not stale:
        return new_id
    if not cleanup or not shown or feature_failed(profile, "delete"):
        print(f"keeping {len(stale)} old image(s)", flush=True)
        return new_id
    try:
        answer = art.delete_list(stale)
    except Exception as e:
        record(profile, "delete", False, f"{type(e).__name__}: {e}")
        return new_id
    # Firmwares word the reply differently, so look at what is really left on the TV.
    left = set(stale) & my_images(art)
    print(f"cleanup {stale} answer={answer} left={sorted(left)}", flush=True)
    record(profile, "delete", not left, f"still on the TV: {sorted(left)}" if left else "")
    state["uploaded"] = [new_id] + sorted(left)
    return new_id


def push_to_tv(cfg, path, state, cleanup, profile):
    # Imported here so that dry runs work without the TV library.
    from samsungtvws import SamsungTVWS

    tv = SamsungTVWS(cfg["tv_host"], port=8002, name="TheFrameVanced", timeout=30)
    try:
        return update_tv(tv.art(), path, state, cleanup, profile)
    finally:
        tv.close()


def print_report(cfg, profile):
    """A table to paste into a compatibility report. Leaves out the TV's address."""
    from samsungtvws import SamsungTVWS

    info = tv_info(cfg["tv_host"]) or {}
    found = {}
    if tv_reachable(cfg["tv_host"]):
        tv = SamsungTVWS(cfg["tv_host"], port=8002, name="TheFrameVanced", timeout=10)
        try:
            found = check_tv(tv.art(), profile)
        finally:
            tv.close()
    names = {True: "works", False: "fails", None: "unknown"}
    print(f'| Model | {info.get("model")} |\n|---|---|')
    print(f'| Frame support | {info.get("frame")} |')
    print(f'| Art API | {found.get("api_version")} |')
    print(f'| Resolution | {info.get("resolution")} |')
    for feature in ("read_status", "list_images", "upload", "show", "delete"):
        entry = profile.get("features", {}).get(feature, {})
        print(f'| {feature} | {names[entry.get("ok")] if entry else "not tried"} {entry.get("detail", "")} |')


def needs_change(cfg, ctx, state, now):
    if not state.get("last_change"):
        return "first run"
    elapsed = (now - datetime.fromisoformat(state["last_change"])).total_seconds() / 60
    if elapsed >= cfg["change_minutes"]:
        return "interval"
    if ctx["band"] != state.get("band"):
        return "time band changed"
    if ctx["weather"] != state.get("weather") and elapsed >= cfg["min_weather_change_minutes"]:
        return "weather changed"
    return None


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="folder with config.json and library/")
    parser.add_argument("--dry-run", action="store_true", help="render a preview, do not touch the TV")
    parser.add_argument("--force", action="store_true", help="change the image now")
    parser.add_argument("--image", help="use this catalog id instead of choosing one")
    parser.add_argument("--report", action="store_true", help="print what works on this TV")
    args = parser.parse_args()
    signal.alarm(RUN_TIMEOUT_SECONDS)

    cfg = load_json(args.data / "config.json", None)
    profile_path = args.data / "tv_profile.json"
    profile = load_json(profile_path, {})
    if args.report:
        print_report(cfg, profile)
        return
    catalog = load_json(args.data / "library" / "catalog.json", [])
    state_path = args.data / "state.json"
    state = load_json(state_path, {})
    now = datetime.now(ZoneInfo(cfg["timezone"]))
    ctx = build_context(cfg, now)
    print(now.isoformat(timespec="minutes"), json.dumps(ctx, ensure_ascii=False), flush=True)

    reason = "forced" if args.force or args.image else needs_change(cfg, ctx, state, now)
    if not reason:
        print("no change needed")
        return
    if not args.dry_run and not tv_reachable(cfg["tv_host"]):
        print("TV is in standby, will retry on the next tick")
        return
    info = None if args.dry_run else tv_info(cfg["tv_host"])
    if info:
        if not info["frame"]:
            print(f'{info["model"]} does not report Art Mode support, nothing to do')
            return
        cfg.setdefault("tv_inches", inches_from_model(info["model"]))

    if args.image:
        item = next(i for i in catalog if i["id"] == args.image)
    else:
        item = pick_image(catalog, ctx, state, cfg.get("disabled_themes", []))
    print(f'{reason}: {item["id"]}', flush=True)
    out_dir = args.data / "output"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / ("preview.jpg" if args.dry_run else "current.jpg")
    render(item, ctx, cfg, args.data, out_path, output_size((info or {}).get("resolution")))
    if args.dry_run:
        print(f"preview written to {out_path}")
        return

    # Save the upload list even if a later step fails, so cleanup still knows about the file.
    try:
        push_to_tv(cfg, out_path, state, (args.data / "cleanup.enabled").exists(), profile)
    finally:
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=1))
    state.update(
        last_change=now.isoformat(),
        band=ctx["band"],
        weather=ctx["weather"],
        kind=item.get("kind"),
        history=(state.get("history", []) + [item["id"]])[-HISTORY_SIZE:],
    )
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
