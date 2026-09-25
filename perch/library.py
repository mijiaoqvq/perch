"""Local library. Every deletion rechecks favourites under the same lock."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import fcntl
import re
import sqlite3
import time

NAME = re.compile(r"wallhaven-([a-z0-9]{6})\.(jpg|png|webp)\Z")


@dataclass(frozen=True)
class Wallpaper:
    path: Path
    wid: str
    size: int
    modified: float
    favorite: bool
    disliked: bool = False
    liked: bool = False


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
            db.execute("CREATE TABLE IF NOT EXISTS dislikes (id TEXT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE TABLE IF NOT EXISTS likes (id TEXT PRIMARY KEY, created TEXT DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS wallpaper_tags (wallpaper_id TEXT, tag_id INTEGER, "
                       "PRIMARY KEY(wallpaper_id, tag_id))")
            db.execute("CREATE TABLE IF NOT EXISTS tag_cache (id TEXT PRIMARY KEY, fetched REAL, "
                       "retry_after REAL NOT NULL DEFAULT 0)")

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
        with self.connect() as db:
            dislikes = {row[0] for row in db.execute("SELECT id FROM dislikes")}
            likes = {row[0] for row in db.execute("SELECT id FROM likes")}
        result = []
        for path in self.directory.iterdir():
            match = NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                stat = path.stat()
                result.append(Wallpaper(path, match[1], stat.st_size, stat.st_mtime,
                                        match[1] in favorites, match[1] in dislikes, match[1] in likes))
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
                db.execute("DELETE FROM dislikes WHERE id=?", (wid,))
            else:
                db.execute("DELETE FROM favorites WHERE id=?", (wid,))

    def cleanup_candidates(self, keep):
        # Retain a disliked original until its replacement has been downloaded.
        ordinary = [item for item in self.items() if not item.favorite and not item.disliked]
        return ordinary[keep:]

    def set_liked(self, wid, value):
        if not re.fullmatch(r"[a-z0-9]{6}", wid):
            raise ValueError("无效的壁纸 ID")
        with self.locked(), self.connect() as db:
            if value:
                if not any(item.wid == wid for item in self.items()):
                    raise FileNotFoundError("壁纸已被清理，请刷新图库")
                db.execute("INSERT OR IGNORE INTO likes(id) VALUES (?)", (wid,))
                db.execute("DELETE FROM dislikes WHERE id=?", (wid,))
            else:
                db.execute("DELETE FROM likes WHERE id=?", (wid,))

    def mark_disliked(self, wid):
        with self.locked(), self.connect() as db:
            item = next((item for item in self.items() if item.wid == wid), None)
            if item is None:
                raise FileNotFoundError("壁纸已不在图库中，请刷新后重试")
            if item.favorite:
                raise ValueError("这张壁纸已收藏，请先取消收藏再点击不喜欢")
            db.execute("INSERT OR IGNORE INTO dislikes(id) VALUES (?)", (wid,))
            db.execute("DELETE FROM likes WHERE id=?", (wid,))

    def feedback(self, include_favorites=True):
        """Feedback survives image cleanup; a liked favourite counts only once."""
        with self.connect() as db:
            values = {row[0]: 1 for row in db.execute("SELECT id FROM likes ORDER BY created, id")}
            if include_favorites:
                values.update({row[0]: 1 for row in db.execute("SELECT id FROM favorites ORDER BY created, id")})
            values.update({row[0]: -1 for row in db.execute("SELECT id FROM dislikes ORDER BY created, id")})
        return values

    def tags_for(self, wid):
        """None means not fetched yet; an empty list is a valid cached response."""
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM tag_cache WHERE id=? AND fetched IS NOT NULL", (wid,)).fetchone():
                return None
            return [dict(id=tid, name=name) for tid, name in db.execute(
                "SELECT tags.id, tags.name FROM tags JOIN wallpaper_tags ON tags.id=tag_id "
                "WHERE wallpaper_id=? ORDER BY tags.name", (wid,))]

    def tags_due(self, wid):
        with self.connect() as db:
            row = db.execute("SELECT fetched, retry_after FROM tag_cache WHERE id=?", (wid,)).fetchone()
            return row is None or (row[0] is None and row[1] <= time.time())

    def save_tags(self, wid, tags):
        if not re.fullmatch(r"[a-z0-9]{6}", wid) or not isinstance(tags, list):
            raise ValueError("无效的壁纸标签")
        normalized = {}
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            tid, name = tag.get("id"), tag.get("name")
            if type(tid) is int and tid > 0 and isinstance(name, str) and name.strip():
                normalized[tid] = name.strip()[:200]
        with self.connect() as db:
            db.execute("DELETE FROM wallpaper_tags WHERE wallpaper_id=?", (wid,))
            for tid, name in normalized.items():
                db.execute("INSERT INTO tags VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name", (tid, name))
                db.execute("INSERT INTO wallpaper_tags VALUES (?, ?)", (wid, tid))
            db.execute("INSERT INTO tag_cache VALUES (?, ?, 0) ON CONFLICT(id) "
                       "DO UPDATE SET fetched=excluded.fetched, retry_after=0", (wid, time.time()))

    def tag_failure(self, wid):
        with self.connect() as db:
            db.execute("INSERT INTO tag_cache(id, retry_after) VALUES (?, ?) ON CONFLICT(id) "
                       "DO UPDATE SET retry_after=excluded.retry_after", (wid, time.time() + 3600))

    def tag_profile(self, include_favorites=True):
        feedback = self.feedback(include_favorites)
        counts = {}
        with self.connect() as db:
            for wid, tid, name in db.execute("SELECT wallpaper_id, tags.id, tags.name FROM wallpaper_tags "
                                             "JOIN tags ON tag_id=tags.id"):
                if wid not in feedback:
                    continue
                tag = counts.setdefault(tid, dict(id=tid, name=name, positive=0, negative=0))
                tag["positive" if feedback[wid] > 0 else "negative"] += 1
        for tag in counts.values():
            pos, neg = tag["positive"], tag["negative"]
            tag["weight"] = (pos - 1.5 * neg) / (pos + neg + 2)
        return sorted(counts.values(), key=lambda tag: (-tag["weight"], tag["id"]))

    def finish_replacement(self, replacement, wid=None):
        """Remove only a disliked original, after the new file is safely installed."""
        replacement = Path(replacement)
        with self.locked():
            if (replacement.parent != self.directory or not NAME.fullmatch(replacement.name)
                    or replacement.is_symlink() or not replacement.is_file()):
                raise ValueError("新壁纸尚未保存，保留原图")
            candidates = [item for item in self.items()
                          if item.disliked and not item.favorite and item.path != replacement
                          and (wid is None or item.wid == wid)]
            if not candidates:
                return False
            selected_id = candidates[-1].wid
            for item in candidates:
                if item.wid == selected_id and not item.path.is_symlink():
                    item.path.unlink(missing_ok=True)
            return True

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
            return db.execute("SELECT 1 FROM seen WHERE id=? OR sha256=? "
                              "UNION ALL SELECT 1 FROM dislikes WHERE id=? LIMIT 1",
                              (wid, digest, wid)).fetchone() is not None

    def remember(self, wid, digest):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO seen VALUES (?, ?)", (wid, digest))
