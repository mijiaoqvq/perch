"""Local library. Every deletion rechecks favourites under the same lock."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import fcntl
import re
import sqlite3

NAME = re.compile(r"wallhaven-([a-z0-9]{6})\.(jpg|png|webp)\Z")


@dataclass(frozen=True)
class Wallpaper:
    path: Path
    wid: str
    size: int
    modified: float
    favorite: bool


class Library:
    def __init__(self, directory, state):
        self.directory = Path(directory)
        self.state = Path(state)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS seen_hash ON seen(sha256)")
            db.execute("CREATE TABLE IF NOT EXISTS favorites (id TEXT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.state / "history.sqlite3", timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def locked(self, name="library.lock", blocking=True):
        with (self.state / name).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def favorite_ids(self):
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT id FROM favorites")}

    def items(self):
        favorites = self.favorite_ids()
        result = []
        for path in self.directory.iterdir():
            match = NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                result.append(Wallpaper(path, match[1], stat.st_size, stat.st_mtime,
                                        match[1] in favorites))
            except FileNotFoundError:
                continue  # A concurrently completed update may have pruned it.
        return sorted(result, key=lambda item: (item.modified, item.path.name), reverse=True)

    def set_favorite(self, wid, value):
        if not re.fullmatch(r"[a-z0-9]{6}", wid):
            raise ValueError("无效的壁纸 ID")
        with self.locked(), self.connect() as db:
            if value:
                if not any(item.wid == wid for item in self.items()):
                    raise FileNotFoundError("壁纸已被清理，请刷新图库")
                db.execute("INSERT OR IGNORE INTO favorites(id) VALUES (?)", (wid,))
            else:
                db.execute("DELETE FROM favorites WHERE id=?", (wid,))

    def cleanup_candidates(self, keep):
        ordinary = [item for item in self.items() if not item.favorite]
        return ordinary[keep:]

    def prune(self, keep, approved=None):
        """Only delete current excess files; optionally intersect a reviewed list."""
        removed = []
        with self.locked():
            for item in self.cleanup_candidates(keep):
                if approved is not None and item.path.name not in approved:
                    continue
                if item.path.is_symlink():
                    continue
                item.path.unlink(missing_ok=True)
                removed.append(item.path.name)
        return removed

    def seen(self, wid=None, digest=None):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM seen WHERE id=? OR sha256=?", (wid, digest)).fetchone() is not None

    def remember(self, wid, digest):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (wid, digest))

