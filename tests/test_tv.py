import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frame  # noqa: E402


KST = ZoneInfo("Asia/Seoul")


class FakeArt:
    """A TV that can be told to behave like other models."""

    def __init__(self, mode="on", photos=("MY_F0001",), **quirks):
        self.mode = mode
        self.photos = list(photos)
        self.current = "SAM-1"
        self.quirks = quirks
        self.deleted = []
        self.selected = []

    def get_artmode(self):
        return self.mode

    def available(self, category=None):
        if category and self.quirks.get("no_category"):
            raise RuntimeError("error -1")
        return [{"content_id": c} for c in self.photos + ["SAM-1"]]

    def upload(self, data, **kwargs):
        new_id = f"MY_F{len(self.photos) + 2:04}"
        if self.quirks.get("upload_fails"):
            raise TimeoutError("no reply")
        self.photos.append(new_id)
        if self.quirks.get("no_confirmation"):
            raise TimeoutError("no image_added event")
        return new_id

    def select_image(self, content_id, show=True):
        self.selected.append((content_id, show))
        if not self.quirks.get("current_stays"):
            self.current = content_id

    def get_current(self):
        return {"content_id": self.current}

    def delete_list(self, ids):
        if self.quirks.get("delete_raises"):
            raise RuntimeError("error -9")
        if not self.quirks.get("delete_ignored"):
            self.deleted.extend(ids)
            self.photos = [p for p in self.photos if p not in ids]
        return self.quirks.get("delete_answer", True)

    def get_api_version(self):
        if self.quirks.get("no_version"):
            raise RuntimeError("error -9")
        return "4.3.4.0"


class UpdateTv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "a.jpg"
        self.image.write_bytes(b"jpeg")
        self.state = {"uploaded": ["MY_F0001"]}
        self.profile = {}

    def run_update(self, art, cleanup=True):
        return frame.update_tv(art, self.image, self.state, cleanup, self.profile)

    def status(self, feature):
        return self.profile["features"][feature]["ok"]

    def test_normal_change_removes_the_old_image(self):
        art = FakeArt()
        new_id = self.run_update(art)
        self.assertEqual(art.deleted, ["MY_F0001"])
        self.assertEqual(self.state["uploaded"], [new_id])
        self.assertEqual(art.selected, [(new_id, True)])
        self.assertTrue(self.status("delete"))

    def test_outside_art_mode_the_screen_is_left_alone(self):
        art = FakeArt(mode="off")
        new_id = self.run_update(art)
        self.assertEqual(art.selected, [(new_id, False)])

    def test_cleanup_off_keeps_old_images(self):
        art = FakeArt()
        self.run_update(art, cleanup=False)
        self.assertEqual(art.deleted, [])

    def test_image_stored_without_confirmation_is_still_tracked(self):
        art = FakeArt(no_confirmation=True)
        new_id = self.run_update(art)
        self.assertEqual(new_id, "MY_F0003")
        self.assertTrue(self.status("upload"))

    def test_failed_upload_is_recorded_and_raised(self):
        art = FakeArt(upload_fails=True)
        with self.assertRaises(TimeoutError):
            self.run_update(art)
        self.assertFalse(self.status("upload"))

    def test_nothing_is_deleted_when_the_new_image_is_not_confirmed(self):
        art = FakeArt(current_stays=True)
        new_id = self.run_update(art)
        self.assertEqual(art.deleted, [])
        self.assertFalse(self.status("show"))
        self.assertIn(new_id, self.state["uploaded"])
        self.assertIn("MY_F0001", self.state["uploaded"])

    def test_unconfirmed_outside_art_mode_is_not_a_failure(self):
        art = FakeArt(mode="off", current_stays=True)
        self.run_update(art)
        self.assertIsNone(self.status("show"))
        self.assertEqual(art.deleted, [])

    def test_delete_is_judged_by_what_is_left_on_the_tv(self):
        art = FakeArt(delete_answer=False)
        new_id = self.run_update(art)
        self.assertTrue(self.status("delete"))
        self.assertEqual(self.state["uploaded"], [new_id])

    def test_delete_that_did_nothing_is_a_failure(self):
        art = FakeArt(delete_ignored=True)
        self.run_update(art)
        self.assertFalse(self.status("delete"))
        self.assertIn("MY_F0001", self.state["uploaded"])

    def test_failed_delete_is_not_tried_again(self):
        art = FakeArt(delete_raises=True)
        self.run_update(art)
        self.assertFalse(self.status("delete"))
        art.quirks["delete_raises"] = False
        self.run_update(art)
        self.assertEqual(art.deleted, [])

    def test_only_uploaded_images_are_ever_deleted(self):
        self.state["uploaded"] = ["SAM-1", "MY_F0001"]
        art = FakeArt()
        self.run_update(art)
        self.assertEqual(art.deleted, ["MY_F0001"])

    def test_model_without_the_photo_folder_id_still_works(self):
        art = FakeArt(no_category=True)
        self.assertEqual(frame.my_images(art), {"MY_F0001"})
        self.run_update(art)
        self.assertEqual(art.deleted, ["MY_F0001"])


class Clock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.image = Path(self.tmp.name) / "a.jpg"
        self.image.write_bytes(b"jpeg")

    def test_next_time_is_on_the_five_minute_grid(self):
        base = datetime(2026, 11, 2, 14, 4, 30, tzinfo=KST)
        cases = {(14, 4, 30): (14, 5), (14, 5, 0): (14, 5), (14, 5, 1): (14, 10), (14, 59, 30): (15, 0)}
        for (h, m, s), expected in cases.items():
            found = frame.next_clock_time(base.replace(hour=h, minute=m, second=s), 5)
            self.assertEqual((found.hour, found.minute, found.second), (*expected, 0))

    def test_outside_art_mode_nothing_is_uploaded_for_the_clock(self):
        art = FakeArt(mode="off")
        state = {"uploaded": ["MY_F0001"]}
        result = frame.update_tv(art, self.image, state, True, {}, art_mode_only=True)
        self.assertIsNone(result)
        self.assertEqual(art.photos, ["MY_F0001"])
        self.assertEqual(art.deleted, [])

    def test_image_changes_only_when_the_time_comes(self):
        art = FakeArt()
        moment = datetime.now(KST) + timedelta(seconds=0.4)
        seen = {}
        original = art.select_image

        def select(content_id, show=True):
            seen["at"] = datetime.now(KST)
            original(content_id, show)

        art.select_image = select
        frame.update_tv(art, self.image, {"uploaded": []}, False, {}, show_at=moment)
        self.assertGreaterEqual(seen["at"], moment)

    def test_a_new_connection_is_used_to_change_the_image(self):
        first, second = FakeArt(), FakeArt()
        second.photos = first.photos
        reopened = []

        def reopen():
            reopened.append(True)
            return second

        moment = datetime.now(KST) + timedelta(seconds=0.1)
        state = {"uploaded": ["MY_F0001"]}
        new_id = frame.update_tv(first, self.image, state, True, {}, show_at=moment, reopen=reopen)
        self.assertEqual(reopened, [True])
        self.assertEqual(first.selected, [])
        self.assertEqual(second.selected, [(new_id, True)])
        self.assertEqual(second.deleted, ["MY_F0001"])

    def test_old_clock_images_are_all_removed_after_a_successful_change(self):
        art = FakeArt(photos=[f"MY_F{n:04}" for n in range(1, 42)])
        state = {"uploaded": list(art.photos)}
        new_id = frame.update_tv(art, self.image, state, True, {})
        self.assertEqual(len(art.deleted), 41)
        self.assertEqual(state["uploaded"], [new_id])

    def test_time_can_go_in_the_caption(self):
        cfg = {"location_label": "서울"}
        ctx = {"term": "입동", "weather_label": "맑음", "temperature": 3, "time": "14:35"}
        layout = {"headline": "{time} · {weather} {temp}°", "caption": "{title}"}
        self.assertEqual(frame.caption_lines({"caption": "그림"}, ctx, cfg, layout)[0], "14:35 · 맑음 3°")


class CheckTv(unittest.TestCase):
    def test_one_unsupported_call_does_not_hide_the_rest(self):
        profile = {}
        found = frame.check_tv(FakeArt(no_version=True), profile)
        self.assertIsNone(found["api_version"])
        self.assertEqual(found["art_mode"], "on")
        self.assertEqual(found["uploaded"], 1)


class Resolution(unittest.TestCase):
    def test_lower_resolution_tv_gets_a_smaller_image(self):
        self.assertEqual(frame.output_size("1920x1080"), (1920, 1080))

    def test_4k_and_unknown_stay_on_the_canvas(self):
        for value in ("3840x2160", "7680x4320", None, "", "unknown"):
            self.assertEqual(frame.output_size(value), frame.CANVAS)


class TextScale(unittest.TestCase):
    def test_small_tv_text_is_capped(self):
        size = frame.effective_layout({"tv_inches": 32})["headline_size"]
        self.assertEqual(size, round(frame.DEFAULT_LAYOUT["headline_size"] * frame.MAX_TEXT_SCALE))


class Models(unittest.TestCase):
    def test_model_names_from_several_years(self):
        cases = {"QN55LS03BAFXZA": 55, "GQ65LS03DAUXZG": 65, "TQ50LS03FAUXXC": 50, "UN32LS03CBFXZA": 32, "QE85LS03BAUXXN": 85}
        for model, inches in cases.items():
            self.assertEqual(frame.inches_from_model(model), inches)


if __name__ == "__main__":
    unittest.main()
