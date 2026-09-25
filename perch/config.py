"""Validated, atomic preferences shared by the GUI and scheduled worker."""
from dataclasses import asdict, dataclass, field
from pathlib import Path
import json
import os
import re
import tempfile


def xdg(variable, fallback):
    return Path(os.environ.get(variable) or Path.home() / fallback)


def config_path():
    return xdg("XDG_CONFIG_HOME", ".config") / "perch/config.json"


def state_path():
    # Intentionally reuse the existing installation's permanent dedup history.
    return xdg("XDG_STATE_HOME", ".local/state") / "wallhaven-anime"


def minute(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("时间请使用 HH:MM 格式，例如 08:30")
    h, m = map(int, value.split(":"))
    return h * 60 + m


def clock(value):
    return f"{value // 60:02d}:{value % 60:02d}"


@dataclass
class Config:
    directory: str = field(default_factory=lambda: str(Path.home() / "Pictures/Wallpapers"))
    keep: int = 20
    batch: int = 3
    enabled: bool = True
    schedule_mode: str = "interval"
    interval_hours: int = 6
    active_start: str = "00:00"
    active_end: str = "23:59"
    daily_times: list = field(default_factory=lambda: ["00:00", "06:00", "12:00", "18:00"])
    catch_up: bool = False
    query: str = ""
    sorting: str = "toplist"
    min_width: int = 3840
    min_height: int = 2160
    ratio: str = "16x9"
    max_pages: int = 100
    wallpaper_backend: str = "auto"

    def validate(self):
        for name, low, high in (("keep", 1, 10000), ("batch", 1, 100),
                                ("interval_hours", 1, 24), ("max_pages", 1, 200),
                                ("min_width", 1, 16384), ("min_height", 1, 16384)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} 必须在 {low}–{high} 之间")
        if self.batch > self.keep:
            raise ValueError("每次新增数量不能大于普通壁纸保留数量")
        if self.schedule_mode not in ("interval", "daily"):
            raise ValueError("未知的更新时间模式")
        if type(self.enabled) is not bool or type(self.catch_up) is not bool:
            raise ValueError("自动更新和补执行选项必须是布尔值")
        minute(self.active_start)
        minute(self.active_end)
        if not isinstance(self.daily_times, list) or not 1 <= len(self.daily_times) <= 24:
            raise ValueError("请填写 1–24 个更新时间，以逗号分隔")
        for value in self.daily_times:
            minute(value)
        if self.sorting not in ("toplist", "date_added", "random", "favorites"):
            raise ValueError("未知的排序方式")
        if self.ratio not in ("16x9", "16x10", "21x9", "any"):
            raise ValueError("未知的宽高比")
        if self.wallpaper_backend not in ("auto", "dms", "awww", "swww", "gnome", "none"):
            raise ValueError("未知的桌面壁纸后端")
        if not isinstance(self.query, str) or len(self.query) > 500:
            raise ValueError("搜索词最多 500 字符")
        if not isinstance(self.directory, str) or not self.directory.strip() or any(
                c in self.directory for c in ("\n", "\r", "\0")):
            raise ValueError("请选择有效的壁纸目录")
        self.directory = str(Path(self.directory).expanduser().absolute())
        return self

    def slots(self):
        """Wall-clock times, anchored to start; supports windows across midnight."""
        if self.schedule_mode == "daily":
            return sorted(set(self.daily_times))
        start, end = minute(self.active_start), minute(self.active_end)
        duration = (end - start) % 1440
        return sorted(clock((start + offset) % 1440)
                      for offset in range(0, duration + 1, self.interval_hours * 60))


def load(path=None):
    path = Path(path or config_path())
    if not path.exists():
        return Config().validate()
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("设置文件必须是一个 JSON 对象")
        unknown = set(data) - set(Config.__dataclass_fields__)
        if unknown:
            raise ValueError("设置文件包含未知字段：" + ", ".join(sorted(unknown)))
        return Config(**data).validate()
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取设置：{path}：{exc}") from exc


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".perch-", delete=False) as temp:
            name = temp.name
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(name, path)
    finally:
        if name:
            Path(name).unlink(missing_ok=True)


def save(config, path=None):
    config.validate()
    atomic_write(path or config_path(), json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n")
