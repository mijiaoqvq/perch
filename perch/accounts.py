"""Read-only Wallhaven collection import with persistent local override precedence."""
from dataclasses import asdict, dataclass, field
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error

from .config import atomic_write, config_path
from . import colors
from .downloader import Client, MAX_BYTES, eligible, inspect_image

LOG = logging.getLogger('perch')
MODES = {'learn': 1, 'like': 2, 'favorite': 3}
MODE_LABELS = {'off': '不同步', 'learn': '仅学习偏好', 'like': '导入为喜欢', 'favorite': '导入为收藏'}


@dataclass
class Account:
    username: str = ''
    api_key: str = field(default='', repr=False)
    automatic: bool = False
    collections: dict = field(default_factory=dict)

    def validate(self, required=False):
        if not isinstance(self.username, str) or (self.username and not re.fullmatch(r'[\w-]{1,64}', self.username, re.ASCII)):
            raise ValueError('请填写有效的 Wallhaven 用户名')
        if required and not self.username:
            raise ValueError('请先填写 Wallhaven 用户名')
        if not isinstance(self.api_key, str) or (self.api_key and not re.fullmatch(r'[a-zA-Z0-9]{1,128}', self.api_key)):
            raise ValueError('API Key 格式不正确，请重新填写')
        if type(self.automatic) is not bool or not isinstance(self.collections, dict):
            raise ValueError('账户同步设置格式不正确')
        for cid, value in self.collections.items():
            if (not isinstance(cid, str) or not cid.isascii() or not cid.isdigit() or not isinstance(value, dict)
                    or value.get('mode') not in MODE_LABELS or not isinstance(value.get('label'), str)):
                raise ValueError('收藏夹设置格式不正确')
        return self


def account_path(config_file=None):
    path = Path(config_file or config_path())
    return path.with_name(path.stem + '-account.json')


def load_account(path=None):
    path = Path(path or account_path())
    if not path.exists():
        return Account()
    try:
        return Account(**json.loads(path.read_text())).validate()
    except (TypeError, ValueError):
        # Do not echo the JSON or credential value into logs or exception messages.
        raise ValueError('账户设置无法读取，请在账户同步中重新保存') from None


def save_account(account, path=None):
    account.validate()
    atomic_write(path or account_path(), json.dumps(asdict(account), ensure_ascii=False, indent=2) + '\n')


def list_collections(account, client=None):
    account.validate(required=True)
    client = client or Client(account.api_key)
    rows = client.collections(account.username)
    result = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        cid = str(item.get('id', ''))
        if cid.isascii() and cid.isdigit():
            result[cid] = dict(label=str(item.get('label') or f'收藏夹 {cid}')[:200],
                               mode=account.collections.get(cid, {}).get('mode', 'off'))
    return result


def sync_collections(config, library, account, client=None):
    account.validate(required=True)
    selected = {cid: row for cid, row in account.collections.items() if row['mode'] in MODES}
    if not selected:
        raise ValueError('请先为至少一个收藏夹选择导入方式')
    client = client or Client(account.api_key)
    summary = dict(imported=0, downloaded=0, skipped=0, unchanged=0)
    # Serialise with downloads and replacements; closing the GUI cannot interrupt this worker.
    with library.locked('run.lock', blocking=False):
        for cid, collection in selected.items():
            mode = MODES[collection['mode']]
            for item in client.collection_items(account.username, cid):
                if not isinstance(item, dict) or not eligible(item, config):
                    summary['skipped'] += 1
                    continue
                wid = item['id']
                with library.connect() as db:
                    previous = db.execute('SELECT mode FROM collection_imports WHERE account=? AND collection=? AND id=?',
                                          (account.username.casefold(), cid, wid)).fetchone()
                    disliked = db.execute('SELECT 1 FROM dislikes WHERE id=?', (wid,)).fetchone()
                    blocked = db.execute('SELECT like_blocked, favorite_blocked FROM sync_exclusions WHERE id=?', (wid,)).fetchone()
                if previous and previous[0] >= mode:
                    summary['unchanged'] += 1
                    continue
                if disliked or (blocked and blocked[0]):
                    summary['skipped'] += 1
                    continue
                LOG.info('正在导入收藏夹 %s · %s', cid, wid)
                if library.tags_for(wid) is None or colors.palette(library, wid) is None:
                    detail = client.detail(wid)
                    library.save_tags(wid, detail['tags'])
                    colors.save_palette(library, wid, detail.get('colors', []))
                local = next((wall for wall in library.items() if wall.wid == wid), None)
                destination = None
                temp_path = None
                # Do not restore cleaned-up ordinary likes. An explicitly chosen favourite import can restore one.
                should_download = mode >= 2 and local is None and (mode == 3 or not library.seen(wid=wid))
                if blocked and blocked[1] and library.seen(wid=wid):
                    should_download = False
                try:
                    if should_download:
                        with tempfile.NamedTemporaryFile(dir=library.directory, prefix='.wallhaven-', suffix='.part', delete=False) as temp:
                            temp_path = Path(temp.name)
                            client.fetch(item['path'], temp, MAX_BYTES)
                            os.fsync(temp.fileno())
                        digest, ext = inspect_image(temp_path, config)
                        destination = library.directory / f'wallhaven-{wid}.{ext}'
                    with library.locked(), library.connect() as db:
                        # Recheck concurrent GUI decisions after the network request.
                        blocked = db.execute('SELECT like_blocked, favorite_blocked FROM sync_exclusions WHERE id=?', (wid,)).fetchone()
                        if db.execute('SELECT 1 FROM dislikes WHERE id=?', (wid,)).fetchone() or (blocked and blocked[0]):
                            summary['skipped'] += 1
                            continue
                        local = next((wall for wall in library.items() if wall.wid == wid), None)
                        if destination and local is None:
                            if destination.exists() or destination.is_symlink():
                                raise ValueError('目标文件已存在，已停止覆盖')
                            os.replace(temp_path, destination)
                            db.execute('INSERT OR IGNORE INTO seen VALUES (?, ?)', (wid, digest))
                            summary['downloaded'] += 1
                        present = local is not None or destination is not None
                        db.execute('INSERT OR IGNORE INTO remote_likes VALUES (?)', (wid,))
                        db.execute('INSERT OR IGNORE INTO feedback_dates VALUES (?, ?)', (wid, time.time()))
                        if mode >= 2:
                            db.execute('INSERT OR IGNORE INTO likes(id) VALUES (?)', (wid,))
                        if mode == 3 and present and not (blocked and blocked[1]):
                            db.execute('INSERT OR IGNORE INTO favorites(id) VALUES (?)', (wid,))
                        db.execute('INSERT INTO collection_imports VALUES (?, ?, ?, ?) ON CONFLICT(account, collection, id) '
                                   'DO UPDATE SET mode=excluded.mode', (account.username.casefold(), cid, wid, mode))
                    summary['imported'] += 1
                finally:
                    if temp_path:
                        temp_path.unlink(missing_ok=True)
        if summary['downloaded']:
            library.prune(config.keep, backend=config.wallpaper_backend)
    LOG.info('收藏夹同步完成 · 新导入 %s · 下载 %s · 筛选或本地反馈跳过 %s · 已同步 %s',
             summary['imported'], summary['downloaded'], summary['skipped'], summary['unchanged'])
    return summary


def friendly_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return {401: 'API Key 无效，请在 Wallhaven 重新获取。',
                403: '无法访问收藏夹，请确认用户名、API Key 和私密收藏夹权限。',
                404: '没有找到该用户或收藏夹，请检查用户名。',
                429: 'Wallhaven 请求过于频繁，请稍后重试。'}.get(error.code, f'Wallhaven 暂不可用（{error.code}）')
    return str(error)
