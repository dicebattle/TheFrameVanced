import gzip
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sources  # noqa: E402


def make_image(path, size=(1920, 1080), color="navy"):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)


class Sources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        (self.data / "library").mkdir(parents=True)
        self.lock = threading.Lock()

    def catalog(self):
        return json.loads((self.data / "library/catalog.json").read_text())

    def folder_source(self, name="가족 사진", theme="가족"):
        folder = Path(self.tmp.name) / "photos"
        make_image(folder / "봄 소풍.jpg")
        make_image(folder / "sub/sea.PNG")
        (folder / "notes.txt").write_text("not an image")
        return sources.add_source(self.data, name, "folder", str(folder), theme)

    def test_folder_images_are_copied_with_the_theme(self):
        source = self.folder_source()
        self.assertEqual(sources.sync(self.data, source, self.lock), (2, 0))
        items = self.catalog()
        self.assertEqual({i["caption"] for i in items}, {"봄 소풍", "sea"})
        self.assertEqual({i["themes"][0] for i in items}, {"가족"})
        for item in items:
            self.assertTrue((self.data / "library" / item["file"]).exists())

    def test_second_sync_only_adds_new_images(self):
        source = self.folder_source()
        sources.sync(self.data, source, self.lock)
        make_image(Path(source["location"]) / "new.jpg")
        self.assertEqual(sources.sync(self.data, source, self.lock), (1, 0))

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            sources.add_source(self.data, "x", "folder", "/no/such/folder", "")
        with self.assertRaises(ValueError):
            sources.add_source(self.data, "x", "image", "file:///etc/passwd", "")
        with self.assertRaises(ValueError):
            sources.add_source(self.data, "x", "other", "https://example.com", "")
        with self.assertRaises(ValueError):
            sources.add_source(self.data, "", "image", "https://example.com/a.jpg", "")

    def test_same_name_is_refused(self):
        self.folder_source()
        with self.assertRaises(ValueError):
            self.folder_source()

    def test_list_cannot_choose_where_files_go(self):
        source = sources.add_source(self.data, "목록", "list", "https://example.com/list.json", "테마")
        image = Path(self.tmp.name) / "a.jpg"
        make_image(image)
        rows = [{
            "id": "../../evil", "url": "https://example.com/a.jpg", "file": "/etc/cron.d/evil",
            "source_id": "other", "caption": "좋은 그림", "unknown": "x", "themes": ["풍경"],
        }]

        def fake_fetch(url):
            return json.dumps(rows).encode() if url.endswith(".json") else image.read_bytes()

        with mock.patch.object(sources, "fetch", fake_fetch):
            self.assertEqual(sources.sync(self.data, source, self.lock), (1, 0))
        item = self.catalog()[0]
        self.assertEqual(item["file"], f'images/{item["id"]}.jpg')
        self.assertNotIn("/", item["id"])
        self.assertNotIn(".", item["id"])
        self.assertEqual(item["source_id"], source["id"])
        self.assertEqual(item["caption"], "좋은 그림")
        self.assertEqual(item["themes"], ["풍경"])
        self.assertNotIn("unknown", item)

    def test_a_file_that_is_not_an_image_is_skipped(self):
        source = sources.add_source(self.data, "주소", "image", "https://example.com/a.jpg", "")
        with mock.patch.object(sources, "fetch", lambda url: b"<html>not an image</html>"):
            self.assertEqual(sources.sync(self.data, source, self.lock), (0, 1))

    def test_large_images_are_shrunk(self):
        target = self.data / "library/images/big.jpg"
        raw = Path(self.tmp.name) / "big.png"
        make_image(raw, (5000, 2800))
        sources.store_image(raw.read_bytes(), target)
        with Image.open(target) as image:
            self.assertEqual(image.width, sources.MAX_WIDTH)

    def test_removing_a_source_keeps_the_original_and_other_images(self):
        source = self.folder_source()
        sources.sync(self.data, source, self.lock)
        path = self.data / "library/catalog.json"
        kept = {"id": "default", "file": "images/default.jpg"}
        path.write_text(json.dumps(self.catalog() + [kept]))
        copies = list((self.data / "library/images").iterdir())
        sources.remove_source(self.data, source["id"], self.lock)
        self.assertEqual(self.catalog(), [kept])
        self.assertEqual(sources.load_sources(self.data), [])
        self.assertFalse(any(c.exists() for c in copies))
        self.assertTrue((Path(source["location"]) / "봄 소풍.jpg").exists())


class Size(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def keeps(self, size):
        raw = self.root / "raw.png"
        make_image(raw, size)
        target = self.root / "out.jpg"
        target.unlink(missing_ok=True)
        try:
            sources.store_image(raw.read_bytes(), target)
        except ValueError:
            return False
        return target.exists()

    def test_only_fhd_or_larger_is_kept(self):
        self.assertTrue(self.keeps((1920, 1080)))
        self.assertTrue(self.keeps((1080, 1920)))
        self.assertFalse(self.keeps((1919, 1080)))
        self.assertFalse(self.keeps((1920, 1079)))
        self.assertFalse(self.keeps((1280, 853)))

    def test_small_images_are_counted_and_the_rest_carry_on(self):
        data = self.root / "data"
        (data / "library").mkdir(parents=True)
        folder = self.root / "photos"
        make_image(folder / "large.jpg")
        make_image(folder / "small.jpg", (800, 450))
        source = sources.add_source(data, "사진", "folder", str(folder), "")
        self.assertEqual(sources.sync(data, source, threading.Lock()), (1, 1))


class Fetch(unittest.TestCase):
    def respond(self, *answers):
        calls = iter(answers)

        def opener(req, timeout):
            answer = next(calls)
            if isinstance(answer, Exception):
                raise answer
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = answer
            return response

        return mock.patch.object(sources.urllib.request, "urlopen", opener)

    def test_compressed_files_are_opened(self):
        with self.respond(gzip.compress(b"image")):
            self.assertEqual(sources.fetch("https://example.com/a"), b"image")

    def test_rate_limits_are_waited_out(self):
        limited = urllib.error.HTTPError("u", 429, "slow", {"Retry-After": "1"}, None)
        with self.respond(limited, b"image"), mock.patch.object(sources.time, "sleep") as sleep:
            self.assertEqual(sources.fetch("https://example.com/a"), b"image")
        sleep.assert_called_once_with(6)

    def test_other_errors_are_not_retried(self):
        missing = urllib.error.HTTPError("u", 404, "gone", {}, None)
        with self.respond(missing), self.assertRaises(urllib.error.HTTPError):
            sources.fetch("https://example.com/a")


class Packs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        (self.data / "library").mkdir(parents=True)
        self.lock = threading.Lock()
        packs = self.root / "packs"
        packs.mkdir()
        (packs / "night.json").write_text(json.dumps({
            "id": "night", "name": "야경", "theme": "야경 묶음", "note": "설명",
            "defaults": {"kind": "photo", "license": "CC BY"},
            "images": [{"id": "one", "url": "https://example.com/1.jpg", "caption": "서울", "provider": "공유마당", "source": "https://example.com/1"}],
        }))
        patcher = mock.patch.object(sources, "PACKS", packs)
        patcher.start()
        self.addCleanup(patcher.stop)
        image = self.root / "image.jpg"
        make_image(image)
        self.image = image.read_bytes()

    def catalog(self):
        return json.loads((self.data / "library/catalog.json").read_text())

    def test_nothing_is_downloaded_until_the_pack_is_pressed(self):
        self.assertEqual(sources.load_sources(self.data), [])
        self.assertFalse((self.data / "library/images").exists())

    def test_pressing_a_pack_downloads_it_with_its_credit(self):
        source = sources.add_pack(self.data, "night")
        with mock.patch.object(sources, "fetch", return_value=self.image):
            self.assertEqual(sources.sync(self.data, source, self.lock), (1, 0))
        item = self.catalog()[0]
        self.assertEqual(item["id"], "night-one")
        self.assertEqual((item["license"], item["provider"], item["source"]), ("CC BY", "공유마당", "https://example.com/1"))
        self.assertEqual(item["source_id"], "night")

    def test_pressing_twice_keeps_one_source(self):
        sources.add_pack(self.data, "night")
        sources.add_pack(self.data, "night")
        self.assertEqual(len(sources.load_sources(self.data)), 1)

    def test_an_unknown_pack_is_refused(self):
        with self.assertRaises(ValueError):
            sources.add_pack(self.data, "../secret")

    def test_removing_a_pack_uses_the_normal_source_removal(self):
        source = sources.add_pack(self.data, "night")
        with mock.patch.object(sources, "fetch", return_value=self.image):
            sources.sync(self.data, source, self.lock)
        sources.remove_source(self.data, "night", self.lock)
        self.assertEqual(self.catalog(), [])

    def test_shipped_packs_hold_no_images_and_have_credits(self):
        with mock.patch.object(sources, "PACKS", Path(sources.__file__).parent / "library/packs"):
            packs = sources.load_packs()
        self.assertEqual({p["id"] for p in packs}, {"stock-night", "gongu-night", "bing", "google-arts"})
        for pack in packs:
            self.assertEqual(pack.get("personal", False), "feed" in pack)
            for row in pack.get("images", []):
                self.assertTrue(row["url"].startswith("https://"))
                self.assertTrue(row["source"].startswith("https://"))

    def test_bing_feed_rows(self):
        feed = {"images": [{
            "urlbase": "/th?id=OHR.Forest_ROW123", "url": "/th?id=OHR.Forest_ROW123_UHD.jpg&w=3840",
            "copyright": "Black Forest, Germany (© Britus/Getty Images)", "copyrightlink": "https://www.bing.com/search?q=x",
        }]}
        with mock.patch.object(sources, "fetch", return_value=json.dumps(feed).encode()):
            row = sources.bing_rows()[0]
        self.assertEqual(row["id"], "Forest")
        self.assertEqual(row["url"], "https://www.bing.com/th?id=OHR.Forest_ROW123_UHD.jpg&w=3840")
        self.assertEqual((row["caption"], row["artist"]), ("Black Forest, Germany", "Britus/Getty Images"))

    def test_google_arts_feed_rows(self):
        feed = [{"title": "Poppies", "creator": "R. Vonnoh", "attribution": "Museum", "image": "http://lh4.ggpht.com/abc", "link": "asset-viewer/9QE0y-f"}]
        with mock.patch.object(sources, "fetch", return_value=json.dumps(feed).encode()):
            row = sources.google_arts_rows()[0]
        self.assertEqual(row["url"], "https://lh4.ggpht.com/abc=s3840")
        self.assertEqual((row["id"], row["provider"]), ("9QE0y-f", "Museum"))


class BaseImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.images = self.data / "library/images"
        self.images.mkdir(parents=True)
        self.catalog_path = self.data / "library/catalog.json"
        self.manifest = root / "manifest.json"
        self.manifest.write_text(json.dumps([
            {"id": "keep", "caption": "새 설명", "url": "https://example.com/keep.jpg"},
            {"id": "new", "caption": "새 사진", "url": "https://example.com/new.jpg"},
        ]))
        self.catalog_path.write_text(json.dumps([
            {"id": "keep", "caption": "옛 설명", "file": "images/keep.jpg", "enabled": False, "focus": [0.5, 0.2]},
            {"id": "old", "file": "images/old.jpg"},
            {"id": "mine-a", "file": "images/mine-a.jpg", "source_id": "mine"},
        ]))
        for name in ("keep", "old", "mine-a"):
            make_image(self.images / f"{name}.jpg")
        source = root / "image.jpg"
        make_image(source)
        self.image = source.read_bytes()

    def rows(self):
        return {i["id"]: i for i in json.loads(self.catalog_path.read_text())}

    def sync(self):
        with mock.patch.object(sources, "fetch", return_value=self.image):
            return sources.sync_base(self.data, threading.Lock(), manifest=self.manifest)

    def test_status_counts_what_a_press_would_change(self):
        self.assertEqual(sources.base_status(self.data, self.manifest), {"total": 2, "new": 1, "retired": 1})

    def test_new_images_arrive_and_retired_ones_stop_showing(self):
        self.assertEqual(self.sync(), (1, 0))
        self.assertEqual(set(self.rows()), {"keep", "new", "mine-a"})
        self.assertEqual(sources.base_status(self.data, self.manifest), {"total": 2, "new": 0, "retired": 0})

    def test_files_of_retired_images_stay(self):
        self.sync()
        self.assertTrue((self.images / "old.jpg").exists())

    def test_the_viewers_choices_survive(self):
        self.sync()
        keep = self.rows()["keep"]
        self.assertEqual((keep["enabled"], keep["focus"], keep["caption"]), (False, [0.5, 0.2], "새 설명"))

    def test_a_failed_download_is_counted_and_not_listed(self):
        with mock.patch.object(sources, "fetch", side_effect=OSError("offline")):
            self.assertEqual(sources.sync_base(self.data, threading.Lock(), manifest=self.manifest), (0, 1))
        self.assertNotIn("new", self.rows())


if __name__ == "__main__":
    unittest.main()
