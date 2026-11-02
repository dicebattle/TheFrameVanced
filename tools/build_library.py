#!/usr/bin/env python3
"""Download the images listed in library/manifest.json and write catalog.json.

Usage: python tools/build_library.py library/manifest.json data/library
Safe to run again: images that already exist are skipped.
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sources  # noqa: E402


def main():
    manifest, library = Path(sys.argv[1]), Path(sys.argv[2])
    library.mkdir(parents=True, exist_ok=True)
    added, failed = sources.sync_base(
        library.parent, threading.Lock(), on_progress=lambda n: print(f"saved {n}", flush=True), manifest=manifest
    )
    print(f"added {added}, failed {failed}")


if __name__ == "__main__":
    main()
