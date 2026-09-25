#!/usr/bin/python3
"""Wallhaven SFW anime: 3 new images per run, fill/retain 20, persistent dedup."""
import argparse
import fcntl
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

from PIL import Image

KEEP = 20
NEW = 3
MAX_BYTES = 100 * 1024 * 1024
NAME = re.compile(r"wallhaven-([a-z0-9]{6})\.(jpg|png|webp)\Z")
warnings.simplefilter("error", Image.DecompressionBombWarning)
LOG = logging.getLogger("wallhaven-anime")


def valid_size(w, h):
    return w >= 3840 and h >= 2160 and w * 9 == h * 16


def inspect_image(path):
    with Image.open(path) as im:
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(im.format)
        if not ext or not valid_size(*im.size) or getattr(im, "n_frames", 1) != 1:
            raise ValueError("requires a still image >= 3840x2160, exact 16:9")
        im.verify()
    with Image.open(path) as im:
        im.load()  # Also detect truncated image data, including JPEGs.
    with open(path, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest, ext


class Client:
    def __init__(self):
        self.last_request = 0.0

    def fetch(self, url, sink, limit):
        """Stream to a seekable file, pace requests and retry transient failures."""
        for attempt in range(3):
            time.sleep(max(0, 1.5 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            sink.seek(0)
            sink.truncate()
            req = urllib.request.Request(url, headers={
                "User-Agent": "ArchAnimeWallpaper/1.0",
                "Accept-Encoding": "identity",
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    length = response.headers.get("Content-Length")
                    if length and int(length) > limit:
                        raise ValueError("response exceeds size limit")
                    total = 0
                    while chunk := response.read(256 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise ValueError("response exceeds size limit")
                        sink.write(chunk)
                    if length and total != int(length):
                        raise http.client.IncompleteRead(b"", int(length) - total)
                sink.flush()
                return
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    if exc.code != 429 and not 500 <= exc.code <= 599:
                        raise
                    delay = 60 if exc.code == 429 else 5 * (attempt + 1)
                    retry_after = exc.headers.get("Retry-After", "")
                    if retry_after.isdigit():
                        delay = max(delay, min(int(retry_after), 300))
                else:
                    delay = 5 * (attempt + 1)
                if attempt == 2:
                    raise
                LOG.warning("Request failed (%s); retry in %ss", exc, delay)
                time.sleep(delay)

    def candidates(self, max_pages):
        # Preserve popularity ranking; broaden the time window only if needed.
        encountered = set()
        for top_range in ("1M", "1y"):
            for page in range(1, max_pages + 1):
                params = dict(categories="010", purity="100", atleast="3840x2160",
                              ratios="16x9", sorting="toplist", order="desc",
                              topRange=top_range, page=page)
                url = "https://wallhaven.cc/api/v1/search?" + urllib.parse.urlencode(params)
                with tempfile.TemporaryFile() as response:
                    self.fetch(url, response, 4 * 1024 * 1024)
                    response.seek(0)
                    result = json.load(response)
                if not isinstance(result.get("data"), list):
                    raise ValueError("unexpected Wallhaven API response")
                LOG.info("Toplist %s page %s: %s candidates", top_range, page, len(result["data"]))
                for item in result["data"]:
                    wid = str(item.get("id", ""))
                    if wid not in encountered:
                        encountered.add(wid)
                        yield item
                if not result["data"] or page >= int(result["meta"]["last_page"]):
                    break


def remember(db, wid, digest):
    with db:
        db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (wid, digest))


def managed_files(directory):
    return sorted((p for p in directory.iterdir()
                   if NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink()),
                  key=lambda p: (p.stat().st_mtime_ns, p.name))


def prune(directory):
    files = managed_files(directory)
    for old in files[:max(0, len(files) - KEEP)]:
        old.unlink()
        LOG.info("Removed oldest: %s", old.name)


def reconcile(directory, db):
    """Recover after interruption; remove corrupt/duplicate managed files only."""
    digests = set()
    ids = set()
    for path in managed_files(directory):
        wid = NAME.fullmatch(path.name)[1]
        try:
            digest, _ = inspect_image(path)
        except (OSError, ValueError, SyntaxError,
                Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            LOG.warning("Removing invalid managed image %s: %s", path.name, exc)
            path.unlink()
            continue
        remember(db, wid, digest)
        if digest in digests or wid in ids:
            path.unlink()
            LOG.info("Removed local duplicate: %s", path.name)
        digests.add(digest)
        ids.add(wid)
    prune(directory)


def update(directory, db, client, max_pages):
    reconcile(directory, db)
    before = len(managed_files(directory))
    required = max(NEW, KEEP - before)
    added = 0
    failures = 0
    LOG.info("Current=%s; new images required=%s; keep=%s", before, required, KEEP)
    for item in client.candidates(max_pages):
        wid = str(item.get("id", ""))
        if not re.fullmatch(r"[a-z0-9]{6}", wid):
            continue
        if item.get("purity") != "sfw" or item.get("category") != "anime":
            continue
        if not valid_size(int(item.get("dimension_x", 0)), int(item.get("dimension_y", 0))):
            continue
        if db.execute("SELECT 1 FROM seen WHERE id=?", (wid,)).fetchone():
            continue
        url = str(item.get("path", ""))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "w.wallhaven.cc":
            continue
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".wallhaven-", suffix=".part",
                                             delete=False) as temp:
                temp_path = Path(temp.name)
                client.fetch(url, temp, MAX_BYTES)
                os.fsync(temp.fileno())
            digest, ext = inspect_image(temp_path)
            if db.execute("SELECT 1 FROM seen WHERE sha256=?", (digest,)).fetchone():
                remember(db, wid, digest)
                LOG.info("Skipped previously downloaded content: %s", wid)
                continue
            destination = directory / f"wallhaven-{wid}.{ext}"
            # Install only after full download and validation. Next startup recovers
            # a file installed just before an interrupted database commit.
            os.replace(temp_path, destination)
            remember(db, wid, digest)
            added += 1
            LOG.info("Added %s (%s/%s)", destination.name, added, required)
        except (urllib.error.URLError, OSError, ValueError, SyntaxError,
                http.client.HTTPException, Image.DecompressionBombError,
                Image.DecompressionBombWarning) as exc:
            failures += 1
            LOG.warning("Failed %s: %s", wid, exc)
            if failures >= 8:
                raise RuntimeError("8 image failures; stopping this run") from exc
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        # Retention errors must fail the service, not be treated as a skipped image.
        prune(directory)
        if added >= required:
            LOG.info("Complete: %s new; %s retained", added, len(managed_files(directory)))
            return 0
    LOG.error("Not enough unseen eligible candidates: added %s/%s; retained %s. "
              "Will try again at the next scheduled run.",
              added, required, len(managed_files(directory)))
    return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        default=Path.home() / "Pictures/Wallpapers")
    parser.add_argument("--state-directory", type=Path,
                        default=Path(os.environ.get("XDG_STATE_HOME") or
                                     Path.home() / ".local/state") / "wallhaven-anime")
    parser.add_argument("--max-pages", type=int, default=100,
                        help="Maximum search pages per popularity window (default: 100)")
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages must be positive")
    args.directory.mkdir(parents=True, exist_ok=True)
    args.state_directory.mkdir(parents=True, exist_ok=True)
    with (args.state_directory / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.info("Another update is running; skipped")
            return 0
        # Only our temporary download files, left by a terminated previous run.
        for partial in args.directory.glob(".wallhaven-*.part"):
            if partial.is_file() or partial.is_symlink():
                partial.unlink()
        db = sqlite3.connect(args.state_directory / "history.sqlite3")
        try:
            db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS seen_hash ON seen(sha256)")
            db.commit()
            return update(args.directory, db, Client(), args.max_pages)
        finally:
            db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        raise SystemExit(main())
    except Exception:
        LOG.exception("Update failed; valid downloaded images and history remain on disk")
        raise SystemExit(1)
