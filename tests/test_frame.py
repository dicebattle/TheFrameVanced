import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frame  # noqa: E402

KST = ZoneInfo("Asia/Seoul")


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=KST)


CTX = {"band": "night", "weather": "clear", "season": "autumn"}


class SolarTerm(unittest.TestCase):
    def test_known_dates(self):
        self.assertEqual(frame.solar_term(at("2026-11-06 12:00")), ("상강", "autumn"))
        self.assertEqual(frame.solar_term(at("2026-11-08 12:00")), ("입동", "winter"))
        self.assertEqual(frame.solar_term(at("2026-02-04 12:00")), ("입춘", "spring"))
        self.assertEqual(frame.solar_term(at("2026-12-22 12:00")), ("동지", "winter"))


class TimeBand(unittest.TestCase):
    sunrise = at("2026-11-02 07:00")
    sunset = at("2026-11-02 17:30")

    def band(self, text):
        return frame.time_band(at(text), self.sunrise, self.sunset)

    def test_boundaries(self):
        self.assertEqual(self.band("2026-11-02 03:00"), "night")
        self.assertEqual(self.band("2026-11-02 06:30"), "dawn")
        self.assertEqual(self.band("2026-11-02 08:00"), "morning")
        self.assertEqual(self.band("2026-11-02 11:00"), "day")
        self.assertEqual(self.band("2026-11-02 16:15"), "sunset")
        self.assertEqual(self.band("2026-11-02 18:05"), "night")


class Weather(unittest.TestCase):
    def test_classes(self):
        cases = {0: "clear", 3: "cloud", 45: "fog", 61: "rain", 71: "snow", 95: "rain"}
        for code, expected in cases.items():
            self.assertEqual(frame.weather_class(code), expected)


class Picking(unittest.TestCase):
    def test_daytime_image_is_not_used_at_night(self):
        item = {"id": "a", "times": ["day"], "seasons": ["any"], "weather": ["any"]}
        self.assertIsNone(frame.score(item, CTX, None))

    def test_winter_image_is_not_used_in_autumn(self):
        item = {"id": "a", "times": ["any"], "seasons": ["winter"], "weather": ["any"]}
        self.assertIsNone(frame.score(item, CTX, None))

    def test_disabled_images_are_skipped(self):
        good = {"id": "good", "times": ["night"], "seasons": ["any"], "weather": ["any"]}
        off = {**good, "id": "off", "enabled": False}
        self.assertEqual(frame.pick_image([off, good], CTX, {})["id"], "good")

    def test_recent_images_are_avoided(self):
        a = {"id": "a", "times": ["night"], "seasons": ["any"], "weather": ["any"]}
        b = {**a, "id": "b"}
        self.assertEqual(frame.pick_image([a, b], CTX, {"history": ["a"]})["id"], "b")


class Themes(unittest.TestCase):
    night = {"id": "n", "times": ["night"], "seasons": ["any"], "weather": ["any"], "themes": ["야경"]}
    space = {**night, "id": "s", "themes": ["우주"]}
    both = {**night, "id": "b", "themes": ["야경", "한국"]}

    def test_turned_off_theme_is_skipped(self):
        self.assertEqual(frame.pick_image([self.night, self.space], CTX, {}, ["야경"])["id"], "s")

    def test_image_stays_while_one_of_its_themes_is_on(self):
        self.assertTrue(frame.in_chosen_themes(self.both, ["야경"]))
        self.assertFalse(frame.in_chosen_themes(self.both, ["야경", "한국"]))

    def test_image_without_themes_is_always_allowed(self):
        self.assertTrue(frame.in_chosen_themes({"id": "x"}, ["야경"]))

    def test_nothing_left_stops_with_a_message(self):
        with self.assertRaises(SystemExit):
            frame.pick_image([self.night], CTX, {}, ["야경"])


class Schedule(unittest.TestCase):
    cfg = {"change_minutes": 180, "min_weather_change_minutes": 60}
    now = at("2026-11-02 12:00")

    def state(self, minutes_ago, band="day", weather="clear"):
        changed = self.now - timedelta(minutes=minutes_ago)
        return {"last_change": changed.isoformat(), "band": band, "weather": weather}

    def test_rules(self):
        ctx = {"band": "day", "weather": "clear"}
        self.assertEqual(frame.needs_change(self.cfg, ctx, {}, self.now), "first run")
        self.assertIsNone(frame.needs_change(self.cfg, ctx, self.state(30), self.now))
        self.assertEqual(frame.needs_change(self.cfg, ctx, self.state(180), self.now), "interval")
        self.assertEqual(
            frame.needs_change(self.cfg, ctx, self.state(30, band="morning"), self.now),
            "time band changed",
        )

    def test_weather_change_waits_for_the_minimum_gap(self):
        ctx = {"band": "day", "weather": "rain"}
        self.assertIsNone(frame.needs_change(self.cfg, ctx, self.state(30), self.now))
        self.assertEqual(frame.needs_change(self.cfg, ctx, self.state(60), self.now), "weather changed")


class Caption(unittest.TestCase):
    def test_unknown_placeholders_stay_as_written(self):
        cfg = {"location_label": "서울"}
        ctx = {"term": "입동", "weather_label": "맑음", "temperature": 3}
        layout = {"headline": "{term} {nope} {temp}°", "caption": "{title}"}
        lines = frame.caption_lines({"caption": "작품"}, ctx, cfg, layout)
        self.assertEqual(lines, ["입동 {nope} 3°", "작품"])


class CaptionColor(unittest.TestCase):
    box = (0, 0, 400, 100)
    layout = {**frame.DEFAULT_LAYOUT}

    def style(self, shade):
        image = frame.Image.new("RGB", (400, 100), (shade, shade, shade))
        return frame.text_style(image, self.box, self.layout)

    def test_bright_background_gets_dark_text(self):
        self.assertIs(self.style(240), frame.TEXT_TONES["dark"])

    def test_dark_background_gets_light_text(self):
        self.assertIs(self.style(40), frame.TEXT_TONES["light"])

    def test_manual_colors_win(self):
        image = frame.Image.new("RGB", (400, 100), "white")
        layout = {**self.layout, "color_mode": "manual", "color": "#112233"}
        self.assertEqual(frame.text_style(image, self.box, layout)["color"], "#112233")


class Veil(unittest.TestCase):
    box = (0, 0, 400, 100)

    def test_plain_dark_wall_gets_no_veil(self):
        image = frame.Image.new("RGB", (400, 100), (25, 27, 30))
        self.assertEqual(frame.veil_strength(image, self.box, False), 0)

    def test_scattered_lights_get_a_veil(self):
        image = frame.Image.new("RGB", (400, 100), (20, 20, 25))
        for x in range(0, 400, 5):
            for y in range(0, 100, 5):
                image.paste((250, 240, 200), (x, y, x + 2, y + 2))
        self.assertGreater(frame.veil_strength(image, self.box, False), 120)
        self.assertIs(frame.text_style(image, self.box, frame.DEFAULT_LAYOUT), frame.TEXT_TONES["light"])


class TvSize(unittest.TestCase):
    def test_inches_come_from_the_model_name(self):
        self.assertEqual(frame.inches_from_model("QN75LS03AAFXZA"), 75)
        self.assertEqual(frame.inches_from_model("QE43LS03FAUXXH"), 43)
        self.assertIsNone(frame.inches_from_model("UNKNOWN"))
        self.assertIsNone(frame.inches_from_model(None))

    def test_smaller_tv_gets_more_pixels_for_the_same_size(self):
        base = frame.DEFAULT_LAYOUT["headline_size"]
        self.assertEqual(frame.effective_layout({"tv_inches": 75})["headline_size"], base)
        self.assertEqual(frame.effective_layout({"tv_inches": 50})["headline_size"], round(base * 1.5))
        self.assertEqual(frame.effective_layout({})["headline_size"], base)


class CaptionPosition(unittest.TestCase):
    def box(self, inches, x=0.031, y=0.895, align="left"):
        layout = {**frame.effective_layout({"tv_inches": inches}), "x": x, "y": y, "align": align}
        lines = ["상강 · 구름 조금 12°", "스위스 스쿠올의 가을 산"]
        return frame.caption_box(frame.caption_rows(lines, layout), layout)[0]

    def test_large_text_stays_inside_the_screen(self):
        for inches in (32, 43, 55, 75, 85):
            for x, y, align in ((0.031, 0.895, "left"), (0.969, 0.99, "right"), (0.0, 0.0, "left")):
                left, top, right, bottom = self.box(inches, x, y, align)
                self.assertGreaterEqual(left, frame.EDGE_MARGIN)
                self.assertGreaterEqual(top, frame.EDGE_MARGIN)
                self.assertLessEqual(right, frame.CANVAS[0] - frame.EDGE_MARGIN)
                self.assertLessEqual(bottom, frame.CANVAS[1] - frame.EDGE_MARGIN)


if __name__ == "__main__":
    unittest.main()
