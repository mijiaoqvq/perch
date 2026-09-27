"""Explicit desktop integration; never execute user-supplied shell commands."""
import os
import ast
import json
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import unquote, urlsplit


class WallpaperStateError(RuntimeError):
    """An unreadable desktop must not be mistaken for an unused wallpaper."""


def _query(command):
    result = subprocess.run(command, capture_output=True, text=True, timeout=4)
    if result.returncode:
        raise ValueError('壁纸接口未响应')
    return result.stdout.rstrip('\r\n')


def _wallpaper_path(value):
    if not isinstance(value, str):
        raise ValueError('无法识别壁纸路径')
    if not value or re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', value) or value.startswith('we:'):
        return None  # Explicitly unset, solid colour or Wallpaper Engine.
    if value.startswith('file:'):
        uri = urlsplit(value)
        if uri.netloc not in ('', 'localhost') or uri.query or uri.fragment:
            raise ValueError('无法识别本地壁纸地址')
        value = unquote(uri.path)
    path = Path(value).expanduser()
    if not path.is_absolute() or '\0' in value or '\n' in value:
        raise ValueError('无法识别壁纸路径')
    return path.resolve()


def current_wallpapers(backend='auto'):
    """Read live state, including every reported monitor; never trust a stale cache."""
    if backend in ('auto', 'none'):
        # Disabling wallpaper *setting* must not disable deletion protection.
        if 'GNOME' in os.environ.get('XDG_CURRENT_DESKTOP', '').upper():
            backend = 'gnome'
        else:
            backend = next((name for name in ('dms', 'awww', 'swww') if shutil.which(name)), 'unknown')
    try:
        if backend == 'dms':
            value = _query(['dms', 'ipc', 'call', 'wallpaper', 'get'])
            if value.upper().startswith('ERROR'):
                if 'per-monitor' not in value.lower():
                    raise ValueError('无法读取 DMS 壁纸')
                data = json.loads(_query(['dms', 'ipc', 'call', 'settings', 'dumpSession']))
                if not isinstance(data, dict) or data.get('perMonitorWallpaper') is not True:
                    raise ValueError('无法识别多显示器壁纸状态')
                monitors = data.get('monitorWallpapers', {})
                if not isinstance(monitors, dict):
                    raise ValueError('无法识别多显示器壁纸路径')
                # wallpaperPath is the fallback for screens without an override.
                values = [data.get('wallpaperPath', ''), *monitors.values()]
            else:
                values = [value]
        elif backend in ('awww', 'swww'):
            text = _query([backend, 'query', *(['--all'] if backend == 'awww' else [])])
            if not text.strip():
                raise ValueError('未取得显示器壁纸状态')
            values = []
            for line in text.splitlines():
                match = re.search(r'currently displaying: (image|color): (.+)$', line)
                if not match:
                    raise ValueError('无法识别显示器壁纸状态')
                if match[1] == 'image':
                    values.append(match[2])
        elif backend == 'gnome':
            values = [ast.literal_eval(_query(['gsettings', 'get', 'org.gnome.desktop.background', key]))
                      for key in ('picture-uri', 'picture-uri-dark')]
        else:
            raise ValueError('未找到支持的壁纸后端')
        return {path for value in values if (path := _wallpaper_path(value)) is not None}
    except (OSError, ValueError, SyntaxError, RuntimeError, subprocess.SubprocessError) as exc:
        raise WallpaperStateError(f'无法读取当前系统壁纸（{backend}），已暂停清理，请确认桌面壁纸服务正在运行') from exc


def set_wallpaper(path, backend="auto"):
    path = path.resolve(strict=True)
    if backend == "auto":
        if shutil.which("dms"):
            backend = "dms"
        elif shutil.which("awww"):
            backend = "awww"
        elif shutil.which("swww"):
            backend = "swww"
        elif "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            backend = "gnome"
        else:
            raise RuntimeError("未找到支持的壁纸后端。可在偏好设置中选择，或打开图片后用桌面工具设置。")
    if backend == "none":
        raise RuntimeError("桌面壁纸设置已关闭，请在偏好设置中选择后端")
    if backend == "dms":
        commands = [["dms", "ipc", "call", "wallpaper", "set", str(path)]]
    elif backend in ("awww", "swww"):
        commands = [[backend, "img", str(path)]]
    elif backend == "gnome":
        commands = [["gsettings", "set", "org.gnome.desktop.background", key, path.as_uri()]
                    for key in ("picture-uri", "picture-uri-dark")]
    else:
        raise ValueError("不支持的桌面壁纸后端")
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        if result.returncode or (backend == "dms" and result.stdout.lstrip().upper().startswith("ERROR")):
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "桌面壁纸后端未启动")
