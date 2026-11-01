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


if __name__ == "__main__":
    unittest.main()
