"""Read desktop appearance choices without modifying system settings."""
import json
import os
from pathlib import Path


def desktop_font_family(config_dir=None, desktop=None):
    desktop = (desktop if desktop is not None else os.environ.get("XDG_CURRENT_DESKTOP", "")).casefold()
    # DMS keeps its font separately from GTK on these supported desktop sessions.
    if not any(name in desktop.split(":") for name in ("niri", "hyprland", "dms", "dankmaterialshell")):
        return None
    root = Path(config_dir or os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        data = json.loads((root / "DankMaterialShell/settings.json").read_text())
        family = data.get("fontFamily") if isinstance(data, dict) else None
        if isinstance(family, str) and family.strip() and len(family) <= 200 and not any(ord(c) < 32 for c in family):
            return family.strip()
    except (OSError, ValueError):
        pass
    return None
