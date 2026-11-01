import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sources  # noqa: E402


def make_image(path, size=(800, 450), color="navy"):
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


if __name__ == "__main__":
    unittest.main()
