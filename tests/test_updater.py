import hashlib
import io
import shutil
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import updater  # noqa: E402


def archive_of(files, prefix="TheFrameVanced-v0.3.0"):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for name, body in files.items():
            info = tarfile.TarInfo(f"{prefix}/{name}" if prefix else name)
            data = body.encode()
            info.size = len(data)
            info.mode = 0o755 if name.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


RELEASE = {"tag": "v0.3.0", "archive": "a", "checksum": "c"}
GOOD_FILES = {"VERSION": "0.3.0\n", "frame.py": "new", "systemd/x.service": "%h/TheFrameVanced/current/x"}


class Versions(unittest.TestCase):
    def test_numbers_compare_as_numbers(self):
        self.assertGreater(updater.parse_version("v0.10.0"), updater.parse_version("v0.9.9"))
        self.assertEqual(updater.parse_version("0.2.0"), updater.parse_version("v0.2.0"))

    def test_other_tags_are_refused(self):
        for tag in ("v1.0.0-beta", "latest", "1.2"):
            with self.assertRaises(updater.UpdateError):
                updater.parse_version(tag)

    def test_only_a_newer_release_counts(self):
        for tag, expected in (("v0.3.0", True), ("v0.2.0", False), ("v0.1.9", False)):
            with mock.patch.object(updater, "latest_release", return_value={"tag": tag}):
                self.assertEqual(bool(updater.newer_release("0.2.0")), expected)


class Download(unittest.TestCase):
    def read(self, archive, checksum):
        return lambda url: archive if url == "a" else checksum

    def test_matching_checksum_is_accepted(self):
        sum_line = f"{hashlib.sha256(b'x').hexdigest()}  file.tar.gz\n".encode()
        self.assertEqual(updater.verified_download(RELEASE, self.read(b"x", sum_line)), b"x")

    def test_wrong_checksum_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.verified_download(RELEASE, self.read(b"tampered", b"0000  file"))

    def test_release_without_checksum_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.verified_download({"tag": "v1.0.0", "archive": "a", "checksum": None}, self.read(b"x", b""))


class Unpack(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "target"
        self.target.mkdir()

    def test_files_land_inside_the_target(self):
        updater.unpack(archive_of({"VERSION": "1", "tools/a.py": "x"}), self.target)
        self.assertTrue((self.target / "tools/a.py").exists())

    def test_parent_paths_are_refused(self):
        with self.assertRaises(updater.UpdateError):
            updater.unpack(archive_of({"../evil.py": "x"}), self.target)
        self.assertFalse((Path(self.tmp.name) / "evil.py").exists())

    def test_links_are_refused(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            info = tarfile.TarInfo("TheFrameVanced-v1/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        with self.assertRaises(updater.UpdateError):
            updater.unpack(out.getvalue(), self.target)


class Install(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "root"
        self.old = self.root / "versions/v0.2.0"
        self.old.mkdir(parents=True)
        (self.old / "VERSION").write_text("0.2.0\n")
        (self.old / "systemd").mkdir()
        (self.root / "current").symlink_to(self.old)
        (self.root / "data").mkdir()
        (self.root / "data/keep.json").write_text("{}")
        units = mock.patch.object(updater, "UNITS", Path(self.tmp.name) / "units")
        units.start()
        self.addCleanup(units.stop)
        self.restarts = 0

    def run_install(self, files=None, check=lambda root, staging: None, healthy=lambda: True):
        def restart():
            self.restarts += 1

        updater.install(
            self.root, RELEASE, lambda *a, **k: None,
            download=lambda release: archive_of(files or GOOD_FILES),
            check=check, restart=restart, healthy=healthy,
        )

    def running(self):
        return (self.root / "current").resolve().name

    def test_success_switches_and_keeps_the_data(self):
        self.run_install()
        self.assertEqual(self.running(), "v0.3.0")
        self.assertTrue(self.old.exists())
        self.assertTrue((self.root / "data/keep.json").exists())
        self.assertEqual(self.restarts, 1)

    def test_service_files_get_the_install_path(self):
        self.run_install()
        unit = (updater.UNITS / "x.service").read_text()
        self.assertEqual(unit, f"{self.root}/current/x")

    def test_failed_tests_leave_the_running_version_alone(self):
        def fail(root, staging):
            raise updater.UpdateError("tests failed")

        with self.assertRaises(updater.UpdateError):
            self.run_install(check=fail)
        self.assertEqual(self.running(), "v0.2.0")
        self.assertEqual(self.restarts, 0)
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["v0.2.0"])

    def test_wrong_version_inside_is_refused(self):
        with self.assertRaises(updater.UpdateError):
            self.run_install({"VERSION": "9.9.9\n"})
        self.assertEqual(self.running(), "v0.2.0")

    def test_page_not_answering_rolls_back(self):
        with self.assertRaisesRegex(updater.UpdateError, "v0.2.0"):
            self.run_install(healthy=lambda: False)
        self.assertEqual(self.running(), "v0.2.0")
        self.assertEqual(self.restarts, 2)

    def test_any_failure_after_the_switch_rolls_back(self):
        calls = []

        def broken_units(root, code_dir):
            calls.append(code_dir.name)
            if len(calls) == 1:
                raise UnicodeDecodeError("utf-8", b"x", 0, 1, "bad unit file")

        with mock.patch.object(updater, "install_units", broken_units):
            with self.assertRaisesRegex(updater.UpdateError, "v0.2.0"):
                self.run_install()
        self.assertEqual(self.running(), "v0.2.0")
        self.assertEqual(calls, ["v0.3.0", "v0.2.0"])

    def test_only_service_files_are_copied(self):
        files = {**GOOD_FILES, "systemd/._x.service": "x", "systemd/notes.txt": "not a unit"}
        self.run_install(files)
        self.assertEqual(sorted(p.name for p in updater.UNITS.iterdir()), ["._x.service", "x.service"])

    def test_installing_the_running_version_is_refused(self):
        self.run_install()
        with self.assertRaises(updater.UpdateError):
            self.run_install()

    def test_old_versions_are_trimmed_but_not_the_ones_in_use(self):
        for tag in ("v0.0.1", "v0.0.2", "v0.1.0"):
            (self.root / "versions" / tag).mkdir()
        self.run_install()
        left = sorted(p.name for p in (self.root / "versions").iterdir())
        self.assertEqual(left, ["v0.1.0", "v0.2.0", "v0.3.0"])


class Layout(unittest.TestCase):
    def test_install_root_needs_the_versions_folder(self):
        self.assertEqual(updater.install_root(Path("/a/versions/v1.0.0")), Path("/a"))
        self.assertIsNone(updater.install_root(Path("/home/me/TheFrameVanced")))

    def test_a_rolled_back_release_is_remembered(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.assertFalse(updater.failed_before(tmp, "v0.3.0"))
        updater.Status(tmp)("error", "no", failed="v0.3.0")
        self.assertTrue(updater.failed_before(tmp, "v0.3.0"))
        self.assertFalse(updater.failed_before(tmp, "v0.3.1"))

    def test_automatic_updates_are_off_unless_turned_on(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        self.assertFalse(updater.auto_update_enabled(tmp))
        (tmp / "data").mkdir()
        (tmp / "data/config.json").write_text("{}")
        self.assertFalse(updater.auto_update_enabled(tmp))
        (tmp / "data/config.json").write_text('{"auto_update": true}')
        self.assertTrue(updater.auto_update_enabled(tmp))


if __name__ == "__main__":
    unittest.main()
