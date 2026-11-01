#!/usr/bin/env python3
"""Check GitHub for a new release and install it next to the running version.

The running version stays untouched until the new one has passed its checksum and
its own tests. If the admin page does not come back afterwards, it is put back.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPOSITORY = "dicebattle/TheFrameVanced"
LATEST_RELEASE = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
USER_AGENT = {"User-Agent": "TheFrameVanced-updater", "Accept": "application/vnd.github+json"}
ADMIN_URL = "http://127.0.0.1:8765/"
UNITS = Path.home() / ".config/systemd/user"
HEALTH_SECONDS = 30
VERSIONS_KEPT = 3
HERE = Path(__file__).resolve().parent


class UpdateError(Exception):
    pass


def parse_version(text):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not match:
        raise UpdateError(f"알 수 없는 버전입니다: {text[:40]}")
    return tuple(int(part) for part in match.groups())


def current_version(code_dir=HERE):
    return (code_dir / "VERSION").read_text().strip()


def install_root(code_dir=HERE):
    """The install folder, or None when this copy was not set up by install.sh."""
    return code_dir.parents[1] if code_dir.parent.name == "versions" else None


def fetch(url):
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def latest_release():
    release = json.loads(fetch(LATEST_RELEASE))
    tag = release["tag_name"]
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    archive = f"TheFrameVanced-{tag}.tar.gz"
    return {
        "tag": tag,
        "url": release.get("html_url"),
        "archive": assets.get(archive),
        "checksum": assets.get(f"{archive}.sha256"),
    }


def verified_download(release, read=fetch):
    if not release["archive"] or not release["checksum"]:
        raise UpdateError("릴리즈에 설치 파일이나 체크섬이 없습니다")
    archive = read(release["archive"])
    expected = read(release["checksum"]).decode().split()[0]
    if hashlib.sha256(archive).hexdigest() != expected:
        raise UpdateError("체크섬이 맞지 않아 설치하지 않습니다")
    return archive


def unpack(archive, target):
    """Extract files only. Anything that would land outside the target is refused."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts[1:]  # The first folder is the archive's own name.
            if not parts or member.isdir():
                continue
            if not member.isfile() or ".." in parts or Path(member.name).is_absolute():
                raise UpdateError(f"설치 파일에 허용되지 않는 항목이 있습니다: {member.name[:60]}")
            path = target.joinpath(*parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, open(path, "wb") as out:
                shutil.copyfileobj(source, out)
            path.chmod(member.mode & 0o755 | 0o600)


def run(command, cwd, timeout):
    done = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise UpdateError((done.stdout + done.stderr)[-300:] or f"{command[0]} failed")


def prepare(root, staging):
    """Install dependencies and run the new version's own tests."""
    python = root / ".venv/bin/python"
    run([str(python), "-m", "pip", "install", "-q", "-r", "requirements.txt"], staging, 600)
    run([str(python), "-m", "unittest", "discover", "tests"], staging, 300)


def switch(root, target):
    """Point current at target in one step, so there is never a moment without a version."""
    link = root / "current"
    temporary = root / "current.new"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def install_units(root, code_dir):
    """Copy the version's service files, in case they changed."""
    UNITS.mkdir(parents=True, exist_ok=True)
    for unit in (code_dir / "systemd").iterdir():
        if unit.suffix in (".service", ".timer"):
            text = unit.read_text().replace("%h/TheFrameVanced", str(root))
            (UNITS / unit.name).write_text(text)


def restart_services():
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False, timeout=30)
    subprocess.run(["systemctl", "--user", "restart", "theframevanced-admin.service"], check=False, timeout=60)
    subprocess.run(["systemctl", "--user", "try-restart", "theframevanced.timer"], check=False, timeout=30)


def admin_answers(seconds=HEALTH_SECONDS):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(ADMIN_URL, timeout=3) as r:
                if r.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def remove_old_versions(root, keep):
    """Keep the newest versions and always the ones in use."""
    versions = root / "versions"
    named = [d for d in versions.iterdir() if d.is_dir() and re.fullmatch(r"v\d+\.\d+\.\d+", d.name)]
    named.sort(key=lambda d: parse_version(d.name), reverse=True)
    for folder in named[VERSIONS_KEPT:]:
        if folder.name not in keep:
            shutil.rmtree(folder, ignore_errors=True)


def install(root, release, status, download=verified_download, check=prepare, restart=restart_services, healthy=admin_answers):
    tag = release["tag"]
    versions = root / "versions"
    target = versions / tag
    previous = (root / "current").resolve()
    if target.exists() and target == previous:
        raise UpdateError(f"{tag}은 이미 설치돼 있습니다")

    status("installing", f"{tag} 내려받는 중")
    archive = download(release)
    staging = Path(tempfile.mkdtemp(prefix=f".{tag}-", dir=versions))
    try:
        unpack(archive, staging)
        if parse_version((staging / "VERSION").read_text()) != parse_version(tag):
            raise UpdateError("설치 파일의 버전이 릴리즈와 다릅니다")
        status("installing", f"{tag} 시험 중")
        check(root, staging)
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    status("installing", f"{tag}로 바꾸는 중")
    # From here on the running version has changed, so any failure has to put it back.
    try:
        switch(root, target)
        install_units(root, target)
        restart()
        if not healthy():
            raise UpdateError("관리 페이지가 응답하지 않습니다")
    except Exception as e:
        switch(root, previous)
        install_units(root, previous)
        restart()
        raise UpdateError(f"{tag} 적용에 실패해 {previous.name}로 되돌렸습니다: {str(e)[:120]}") from e
    remove_old_versions(root, {target.name, previous.name})


class Status:
    def __init__(self, root):
        self.path = (root / "data/update_status.json") if root else None

    def __call__(self, state, message, **extra):
        print(f"{state}: {message}", flush=True)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            at = datetime.now().isoformat(timespec="minutes")
            self.path.write_text(json.dumps({"state": state, "message": message, "at": at, **extra}, ensure_ascii=False))


def newer_release(version):
    release = latest_release()
    return release if parse_version(release["tag"]) > parse_version(version) else None


def failed_before(root, tag):
    """A release that had to be rolled back is not retried by itself every night."""
    path = root / "data/update_status.json"
    return path.exists() and json.loads(path.read_text()).get("failed") == tag


def auto_update_enabled(root):
    path = root / "data/config.json"
    return path.exists() and json.loads(path.read_text()).get("auto_update") is True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="tell whether a new release exists")
    group.add_argument("--install", action="store_true", help="install the new release now")
    group.add_argument("--auto", action="store_true", help="install only if automatic updates are on")
    args = parser.parse_args()

    root = install_root()
    status = Status(root)
    version = current_version()
    tag = None
    try:
        release = newer_release(version)
        if args.check or not release:
            found = release["tag"] if release else None
            status("idle", f"새 버전 {found}이 있습니다" if found else f"최신 버전입니다 (v{version})", latest=found)
            return
        if root is None:
            raise UpdateError("install.sh로 설치한 경우에만 스스로 업데이트할 수 있습니다")
        if args.auto and (not auto_update_enabled(root) or failed_before(root, release["tag"])):
            if not failed_before(root, release["tag"]):
                status("idle", f"새 버전 {release['tag']}이 있습니다", latest=release["tag"])
            return
        tag = release["tag"]
        install(root, release, status)
        status("done", f"{tag}로 업데이트했습니다")
    except (UpdateError, OSError, ValueError, KeyError, subprocess.SubprocessError) as e:
        status("error", str(e)[:300], failed=tag)
        sys.exit(1)


if __name__ == "__main__":
    main()
