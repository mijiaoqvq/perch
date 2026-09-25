"""Keep one user timer: the original service name remains compatible."""
from pathlib import Path
import subprocess

from .config import atomic_write, config_path, save, xdg

TIMER = "wallhaven-anime.timer"
SERVICE = "wallhaven-anime.service"


def systemctl(*args):
    result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "无法连接用户定时服务")
    return result.stdout.strip()


def timer_text(config):
    config.validate()
    entries = "\n".join(f"OnCalendar=*-*-* {slot}:00" for slot in config.slots())
    return ("# Managed by 栖景 · Perch\n[Timer]\nOnCalendar=\n" + entries +
            f"\nPersistent={'true' if config.catch_up else 'false'}\nAccuracySec=1min\nRandomizedDelaySec=0\n")


def apply(config):
    """Roll back files and enablement if applying a new schedule fails."""
    installed = Path.home() / ".local/share/perch/perch/__init__.py"
    if not installed.exists():
        raise RuntimeError("请先运行项目中的 bash install.sh，以便关闭窗口后仍可自动更新")
    path = config_path()
    override = xdg("XDG_CONFIG_HOME", ".config") / f"systemd/user/{TIMER}.d/perch.conf"
    previous = {p: p.read_text() if p.exists() else None for p in (path, override)}
    enabled = systemctl("show", TIMER, "--property=UnitFileState", "--value") == "enabled"
    active = systemctl("show", TIMER, "--property=ActiveState", "--value") == "active"
    try:
        # Stop the timer first: it must never run a half-applied configuration.
        systemctl("stop", TIMER)
        save(config, path)
        atomic_write(override, timer_text(config))
        systemctl("daemon-reload")
        if config.enabled:
            systemctl("enable", TIMER)
            systemctl("restart", TIMER)
        else:
            systemctl("disable", "--now", TIMER)
    except Exception as exc:
        for p, text in previous.items():
            if text is None:
                p.unlink(missing_ok=True)
            else:
                atomic_write(p, text)
        try:
            systemctl("daemon-reload")
            systemctl("enable" if enabled else "disable", TIMER)
            systemctl("start" if active else "stop", TIMER)
        except Exception as rollback:
            raise RuntimeError(f"应用设置失败：{exc}；恢复定时器失败：{rollback}") from exc
        raise


def status():
    output = systemctl("show", TIMER, "--property=LoadState,ActiveState,NextElapseUSecRealtime")
    data = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if data.get("LoadState") != "loaded":
        return "尚未安装定时服务"
    if data.get("ActiveState") != "active":
        return "自动更新已暂停"
    next_time = data.get("NextElapseUSecRealtime", "")
    return f"下次更新 · {next_time}" if next_time else "自动更新已启用"
