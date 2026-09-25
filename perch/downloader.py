"""Wallhaven downloads with persistent deduplication and protected favourites."""
import hashlib
import http.client
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

from PIL import Image
from . import __version__

MAX_BYTES = 100 * 1024 * 1024
LOG = logging.getLogger("perch")
warnings.simplefilter("error", Image.DecompressionBombWarning)
IMAGE_ERRORS = (OSError, ValueError, SyntaxError, Image.DecompressionBombError,
                Image.DecompressionBombWarning)


def valid_size(w, h, config):
    if w < config.min_width or h < config.min_height:
        return False
    if config.ratio == "any":
        return True
    rw, rh = map(int, config.ratio.split("x"))
    return w * rh == h * rw


def inspect_image(path, config=None):
    with Image.open(path) as im:
        ext = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(im.format)
        if not ext or getattr(im, "n_frames", 1) != 1:
            raise ValueError("需要 JPEG、PNG 或 WebP 静态图片")
        if config is not None and not valid_size(*im.size, config):
            raise ValueError("图片尺寸或比例不符合当前筛选条件")
        im.verify()
    with Image.open(path) as im:
        im.load()
    with open(path, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest, ext


class Client:
    def __init__(self):
        self.last_request = 0.0

    def fetch(self, url, sink, limit):
        for attempt in range(3):
            time.sleep(max(0, 1.5 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            sink.seek(0)
            sink.truncate()
            req = urllib.request.Request(url, headers={
                "User-Agent": f"Perch/{__version__} (Wallhaven wallpaper manager)",
                "Accept-Encoding": "identity",
            })
            try:
                with urllib.request.urlopen(req, timeout=30) as response:
                    length = response.headers.get("Content-Length")
                    if length and int(length) > limit:
                        raise ValueError("图片超过下载大小上限")
                    total = 0
                    while chunk := response.read(256 * 1024):
                        total += len(chunk)
                        if total > limit:
                            raise ValueError("图片超过下载大小上限")
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
                LOG.warning("请求失败（%s），%s 秒后重试", exc, delay)
                time.sleep(delay)

    def candidates(self, config):
        encountered = set()
        seed = None
        ranges = ("1M", "1y") if config.sorting == "toplist" else (None,)
        for top_range in ranges:
            for page in range(1, config.max_pages + 1):
                params = dict(categories="010", purity="100", q=config.query,
                              atleast=f"{config.min_width}x{config.min_height}",
                              sorting=config.sorting, order="desc", page=page)
                if config.ratio != "any":
                    params["ratios"] = config.ratio
                if top_range:
                    params["topRange"] = top_range
                if seed:
                    params["seed"] = seed
                url = "https://wallhaven.cc/api/v1/search?" + urllib.parse.urlencode(params)
                with tempfile.TemporaryFile() as response:
                    self.fetch(url, response, 4 * 1024 * 1024)
                    response.seek(0)
                    result = json.load(response)
                if not isinstance(result, dict) or not isinstance(result.get("data"), list):
                    raise ValueError("Wallhaven 返回了无法识别的数据")
                meta = result.get("meta") or {}
                if config.sorting == "random":
                    seed = meta.get("seed", seed)
                LOG.info("正在浏览第 %s 页 · %s 个候选", page, len(result["data"]))
                for item in result["data"]:
                    if not isinstance(item, dict):
                        continue
                    wid = str(item.get("id", ""))
                    if wid not in encountered:
                        encountered.add(wid)
                        yield item
                if not result["data"] or page >= int(meta.get("last_page", page)):
                    break


def reconcile(library):
    """Index existing images without deleting them, even after filter changes."""
    for item in library.items():
        # Newly disliked images must still have their content hash indexed.
        with library.connect() as db:
            indexed = db.execute("SELECT 1 FROM seen WHERE id=?", (item.wid,)).fetchone()
        if indexed:
            continue
        try:
            digest, _ = inspect_image(item.path)
            library.remember(item.wid, digest)
        except IMAGE_ERRORS as exc:
            LOG.warning("无法读取 %s，已保留原文件：%s", item.path.name, exc)


def update(config, library, client=None):
    return _run(config, library, client)[0]


def replace_wallpaper(config, library, wid, client=None):
    """Replace exactly one selected wallpaper without pruning other images."""
    if not isinstance(wid, str) or not re.fullmatch(r"[a-z0-9]{6}", wid):
        raise ValueError("无效的壁纸 ID")
    result, path = _run(config, library, client, replacement_id=wid)
    if result:
        raise RuntimeError("暂时没有下载到合适的新图，原图已保留；可点击重试或等待下次自动更新")
    return path


def _run(config, library, client=None, replacement_id=None):
    client = client or Client()
    # Same run.lock as the legacy worker, preventing overlap during migration.
    if replacement_id:
        LOG.info("准备替换 %s；如有更新任务，将等待其完成", replacement_id)
    with library.locked("run.lock", blocking=bool(replacement_id)):
        if replacement_id:
            library.mark_disliked(replacement_id)
        for partial in library.directory.glob(".wallhaven-*.part"):
            if partial.is_file() or partial.is_symlink():
                partial.unlink()
        reconcile(library)
        before = sum(not item.favorite for item in library.items())
        required = 1 if replacement_id else max(config.batch, config.keep - before)
        added = failures = 0
        LOG.info("开始更新 · 普通壁纸 %s 张 · 计划新增 %s 张", before, required)
        for item in client.candidates(config):
            wid = str(item.get("id", ""))
            if not re.fullmatch(r"[a-z0-9]{6}", wid):
                continue
            if item.get("purity") != "sfw" or item.get("category") != "anime":
                continue
            try:
                if not valid_size(int(item.get("dimension_x", 0)), int(item.get("dimension_y", 0)), config):
                    continue
            except (ValueError, TypeError):
                continue
            if library.seen(wid=wid):
                continue
            url = str(item.get("path", ""))
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme != "https" or parsed.hostname != "w.wallhaven.cc" or parsed.username:
                continue
            temp_path = None
            try:
                with tempfile.NamedTemporaryFile(dir=library.directory, prefix=".wallhaven-", suffix=".part",
                                                 delete=False) as temp:
                    temp_path = Path(temp.name)
                    client.fetch(url, temp, MAX_BYTES)
                    os.fsync(temp.fileno())
                digest, ext = inspect_image(temp_path, config)
                if library.seen(digest=digest):
                    library.remember(wid, digest)
                    LOG.info("跳过历史重复图片：%s", wid)
                    continue
                destination = library.directory / f"wallhaven-{wid}.{ext}"
                with library.locked():
                    # Do not replace any local image, especially an unindexed favourite.
                    if any(local.wid == wid for local in library.items()):
                        library.remember(wid, digest)
                        continue
                    os.replace(temp_path, destination)
                    library.remember(wid, digest)
                added += 1
                LOG.info("已下载 %s · %s / %s", wid, added, required)
            except (*IMAGE_ERRORS, urllib.error.URLError, http.client.HTTPException) as exc:
                failures += 1
                LOG.warning("下载 %s 失败：%s", wid, exc)
                if failures >= 8:
                    raise RuntimeError("8 张图片下载失败，已停止本次更新") from exc
                continue
            finally:
                if temp_path is not None:
                    temp_path.unlink(missing_ok=True)
            # Never discard existing wallpapers before the first successful download.
            if added:
                if replacement_id:
                    if not library.finish_replacement(destination, replacement_id):
                        raise RuntimeError("原图已收藏或状态发生变化，已保留新图并停止移除原图")
                    LOG.info("不喜欢 · 已将 %s 替换为 %s，原图不再推荐", replacement_id, wid)
                    return 0, destination
                if library.finish_replacement(destination):
                    LOG.info("已用新壁纸替换之前标记为不喜欢的图片")
                for name in library.prune(config.keep):
                    LOG.info("清理旧壁纸：%s", name)
            if added >= required:
                LOG.info("更新完成 · 新增 %s 张 · 收藏已保护", added)
                return 0, None
        LOG.warning("候选不足 · 已新增 %s / %s 张，下次计划将继续尝试", added, required)
        return 1, None
