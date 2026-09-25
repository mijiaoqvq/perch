#!/usr/bin/env bash
set -euo pipefail
if (( EUID == 0 )); then
    echo '请以普通用户运行，不要使用 sudo。' >&2
    exit 1
fi
if [[ ! -f "$HOME/.local/share/perch/perch/__init__.py" ]]; then
    echo '没有找到栖景安装，未删除任何文件。' >&2
    exit 1
fi
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
systemctl --user disable --now wallhaven-anime.timer
systemctl --user stop wallhaven-anime.service
rm -f -- "$unit_dir/wallhaven-anime.timer" "$unit_dir/wallhaven-anime.service" "$unit_dir/wallhaven-anime.timer.d/perch.conf"
rm -f -- "$HOME/.local/bin/perch" "$HOME/.local/bin/wallhaven-anime.py"
rm -f -- "$data_dir/applications/io.github.mijiaoqvq.Perch.desktop" "$data_dir/icons/hicolor/scalable/apps/io.github.mijiaoqvq.Perch.svg"
rm -rf -- "$HOME/.local/share/perch"
systemctl --user daemon-reload
echo '栖景已卸载。壁纸、收藏、下载历史、设置和原安装备份已保留。'
