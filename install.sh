#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if (( EUID == 0 )); then
    echo '请以普通用户运行 bash install.sh，不要使用 sudo。' >&2
    exit 1
fi
/usr/bin/python3 - <<'PY'
try:
    import gi
    from PIL import Image
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw
except (ImportError, ValueError):
    raise SystemExit('缺少依赖。在 Arch 上运行：sudo pacman -S --needed python python-pillow python-gobject gtk4 libadwaita')
PY
exec /usr/bin/python3 -m perch.install "$@"
