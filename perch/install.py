"""User installation with backups and rollback; no picture data is modified."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess

from .config import atomic_write, config_path, load, save, xdg
from . import scheduler


def main():
    root = Path(__file__).resolve().parent.parent
    home = Path.home()
    package = home / '.local/share/perch'
    bins = home / '.local/bin'
    units = xdg('XDG_CONFIG_HOME', '.config') / 'systemd/user'
    applications = xdg('XDG_DATA_HOME', '.local/share') / 'applications'
    icons = xdg('XDG_DATA_HOME', '.local/share') / 'icons/hicolor/scalable/apps'
    dropin = units / 'wallhaven-anime.timer.d/perch.conf'
    targets = [package, bins / 'perch', bins / 'wallhaven-anime.py',
               units / scheduler.SERVICE, units / scheduler.TIMER, dropin,
               config_path(), applications / 'io.github.mijiaoqvq.Perch.desktop',
               icons / 'io.github.mijiaoqvq.Perch.svg']
    # Keep customized service command lines from silently overriding GUI settings.
    custom = units / 'wallhaven-anime.service.d'
    if custom.exists() and list(custom.glob('*.conf')):
        raise SystemExit(f'发现自定义服务覆盖配置：{custom}\n请先备份并移除该覆盖配置，再安装，以免它覆盖 GUI 中的目录设置。')
    active = scheduler.systemctl('show', scheduler.TIMER, '--property=ActiveState', '--value') == 'active'
    enabled = scheduler.systemctl('show', scheduler.TIMER, '--property=UnitFileState', '--value') == 'enabled'
    old_service = scheduler.systemctl('show', scheduler.SERVICE, '--property=LoadState', '--value') == 'loaded'
    config = load()
    if not config_path().exists() and old_service:
        config.enabled = enabled
        config.catch_up = scheduler.systemctl('show', scheduler.TIMER, '--property=Persistent', '--value') == 'yes'
    config.validate()
    backup = xdg('XDG_STATE_HOME', '.local/state') / 'perch/backups' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True)
    records = []
    for i, path in enumerate(targets):
        saved = backup / str(i)
        if path.exists():
            if path.is_dir():
                shutil.copytree(path, saved)
            else:
                shutil.copy2(path, saved)
        records.append({'path': str(path), 'backup': str(saved) if path.exists() else None})
    (backup / 'manifest.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    try:
        if old_service:
            scheduler.systemctl('stop', scheduler.TIMER, scheduler.SERVICE)
        package.mkdir(parents=True, exist_ok=True)
        shutil.copytree(root / 'perch', package / 'perch', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copytree(root / 'assets', package / 'assets', dirs_exist_ok=True)
        launcher = '#!/usr/bin/python3\nimport sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path.home() / ".local/share/perch"))\nfrom perch.__main__ import main\nraise SystemExit(main())\n'
        atomic_write(bins / 'perch', launcher)
        os.chmod(bins / 'perch', 0o755)
        bins.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / 'wallhaven-anime.py', bins / 'wallhaven-anime.py')
        for name in (scheduler.SERVICE, scheduler.TIMER):
            units.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, units / name)
        save(config)
        atomic_write(dropin, scheduler.timer_text(config))
        desktop = (root / 'assets/io.github.mijiaoqvq.Perch.desktop').read_text()
        executable = str(bins / 'perch').replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
        atomic_write(applications / 'io.github.mijiaoqvq.Perch.desktop', desktop.replace('Exec=perch', f'Exec="{executable}"'))
        icons.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / 'assets/io.github.mijiaoqvq.Perch.svg', icons / 'io.github.mijiaoqvq.Perch.svg')
        scheduler.systemctl('daemon-reload')
        scheduler.systemctl('enable' if config.enabled else 'disable', scheduler.TIMER)
        if config.enabled:
            scheduler.systemctl('start', scheduler.TIMER)
    except Exception:
        for record in reversed(records):
            path = Path(record['path'])
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            if record['backup']:
                source = Path(record['backup'])
                path.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, path)
                else:
                    shutil.copy2(source, path)
        scheduler.systemctl('daemon-reload')
        if old_service:
            scheduler.systemctl('enable' if enabled else 'disable', scheduler.TIMER)
            if active:
                scheduler.systemctl('start', scheduler.TIMER)
        raise
    if shutil.which('update-desktop-database'):
        subprocess.run(['update-desktop-database', str(applications)], check=False)
    print(f'栖景 · Perch 已安装。\n从应用菜单打开「栖景」，或运行 {bins / "perch"}\n原安装备份：{backup}\n壁纸和下载历史继续沿用，不会重复创建定时器。')


if __name__ == '__main__':
    main()
