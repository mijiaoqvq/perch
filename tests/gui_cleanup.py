"""Native cleanup confirmation with a changing, isolated desktop wallpaper."""
from pathlib import Path
import os
import sys
import tempfile
import traceback
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch.config import Config, save
from perch.desktop import WallpaperStateError
from perch.library import Library
from perch.gui import Application, Adw, Gtk, GLib

root = Path(tempfile.mkdtemp(prefix='perch-cleanup-gui-'))
library = Library(root / 'images', root / 'state')
paths = []
for index in range(5):
    path = library.directory / f'wallhaven-test0{index}.png'
    Image.new('RGB', (320, 180), (40 + 20 * index, 70, 90)).save(path)
    os.utime(path, (index + 1, index + 1))
    paths.append(path)
config = Config(directory=str(library.directory), keep=2, batch=1)
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
stage, attempts, messages, errors = 0, 0, [], []


def fail(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()
sys.excepthook = fail


def check():
    global stage, attempts
    attempts += 1
    if attempts > 25:
        raise AssertionError(f'Cleanup GUI timed out: {stage}')
    win = app.window
    if not win or win.refresh_busy or getattr(win, 'cleanup_preview_busy', False):
        return True
    if stage == 0:
        win.toast = messages.append
        win.preview_cleanup()
        stage = 1
    elif stage == 1:
        dialogs = [w for w in Gtk.Window.list_toplevels() if isinstance(w, Adw.MessageDialog) and w.get_visible()]
        if not dialogs:
            return True
        dialog = dialogs[0]
        assert paths[0].name not in dialog.get_body()
        assert paths[1].name in dialog.get_body() and paths[2].name in dialog.get_body()
        assert '当前桌面壁纸全部保留' in dialog.get_body()
        active.return_value = {paths[1]}
        def widgets(parent):
            yield parent
            child = parent.get_first_child()
            while child:
                yield from widgets(child)
                child = child.get_next_sibling()
        confirm = next(w for w in widgets(dialog) if isinstance(w, Gtk.Button) and w.get_label() == '清理这些壁纸')
        confirm.emit('clicked')
        stage = 2
    elif stage == 2:
        if paths[2].exists():
            return True
        assert all(path.exists() for path in [paths[0], paths[1], paths[3], paths[4]])
        assert any('当前桌面壁纸已保护' in text for text in messages)
        active.side_effect = WallpaperStateError('无法读取当前系统壁纸，已暂停清理')
        win.preview_cleanup()
        stage = 3
    elif stage == 3:
        assert any('已暂停清理' in text for text in messages)
        assert len(library.items()) == 4
        assert not any(isinstance(w, Adw.MessageDialog) and w.get_visible() for w in Gtk.Window.list_toplevels())
        print('GUI PASS: asynchronous preview, current wallpaper excluded, desktop change after confirmation, unreadable desktop blocks cleanup', flush=True)
        stage = 4
        app.quit()
        return False
    return True


GLib.timeout_add(900, check)
with patch('perch.library.current_wallpapers', return_value={paths[0]}) as active:
    app.run([])
raise SystemExit(1 if errors or stage < 4 else 0)
