#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if (( EUID == 0 )); then
    echo '请用普通用户运行 bash install.sh；不要使用 sudo。' >&2
    exit 1
fi
/usr/bin/python3 -c 'from PIL import Image' || {
    echo '请先执行：sudo pacman -Syu --needed python python-pillow' >&2
    exit 1
}
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
install -Dm755 wallhaven-anime.py "$HOME/.local/bin/wallhaven-anime.py"
install -Dm644 wallhaven-anime.service "$unit_dir/wallhaven-anime.service"
install -Dm644 wallhaven-anime.timer "$unit_dir/wallhaven-anime.timer"
systemctl --user daemon-reload
systemctl --user enable --now wallhaven-anime.timer
systemctl --user start --no-block wallhaven-anime.service
echo '已启用定时器，并开始首次补全。查看日志：'
echo 'journalctl --user -u wallhaven-anime.service -f'
