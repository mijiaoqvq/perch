"""Explicit desktop integration; never execute user-supplied shell commands."""
import os
import shutil
import subprocess


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
