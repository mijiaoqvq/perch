"""Isolated GTK interaction checks for account import, tones and conditional stars."""
import json
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch
from types import SimpleNamespace

from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch.config import Config, save
from perch.library import Library
from perch.gui import Application, Gtk, GLib
from perch import colors, accounts
from gui_support import capture_widget
from test_accounts import CollectionClient

root = Path(tempfile.mkdtemp(prefix='perch-sync-gui-'))
library = Library(root / 'images', root / 'state')
Image.new('RGB', (960, 540), '#587766').save(library.directory / 'wallhaven-demo00.png')
config = Config(directory=str(library.directory), min_width=32, min_height=18)
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
errors, stage, attempts, preview = [], 0, 0, None


def fail(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()
sys.excepthook = fail


def descendants(widget):
    yield widget
    child = widget.get_first_child()
    while child:
        yield from descendants(child)
        child = child.get_next_sibling()


def stars(widget):
    return [w for w in descendants(widget) if isinstance(w, Gtk.Button)
            and w.get_icon_name() in ('starred-symbolic', 'non-starred-symbolic')]


def fake_sync(command, **kwargs):
    assert command[3] == 'sync-collections'
    account = accounts.load_account(accounts.account_path(root / 'config.json'))
    stats = accounts.sync_collections(config, library, account, CollectionClient())
    kwargs['stdout'].write(json.dumps(stats).encode())
    kwargs['stdout'].flush()
    return SimpleNamespace(returncode=0, poll=lambda: 0)


def check():
    global stage, attempts, preview
    attempts += 1
    if attempts > 65:
        raise AssertionError(f'GUI timed out at {stage}')
    win = app.window
    if not win or win.refresh_busy or win.color_edit_busy:
        return True
    if stage == 0:
        assert not stars(win.flow), 'Unliked wallpaper must not show a favourite button'
        win.like_button(win.items[0]).emit('clicked')
        stage = 1
    elif stage == 1:
        if not win.items[0].liked:
            return True
        assert len(stars(win.flow)) == 1
        stars(win.flow)[0].emit('clicked')
        stage = 2
    elif stage == 2:
        if not win.items[0].favorite:
            return True
        win.like_button(win.items[0]).emit('clicked')
        stage = 3
    elif stage == 3:
        if win.items[0].liked:
            return True
        assert not win.items[0].favorite and win.items[0].path.exists()
        assert not stars(win.flow)
        preview = win.preview(win.items[0])
        win.like_button(win.items[0], preview).emit('clicked')
        stage = 4
    elif stage == 4:
        if not win.items[0].liked or preview.get_visible():
            return True
        previews = [w for w in Gtk.Window.list_toplevels() if w.get_visible() and w.get_title() == 'DEMO00 · 栖景']
        assert len(previews) == 1, 'Liking must keep a usable preview open'
        assert any(isinstance(w, Gtk.Label) and w.get_text() == '收藏壁纸' for w in descendants(previews[0]))
        previews[0].close()
        win.show_color_manager()
        stage = 5
    elif stage == 5:
        if len(win.color_widgets) != 12:
            return True
        assert win.gallery_stack.get_visible_child_name() == 'colors'
        assert not win.gallery_hero.get_visible() and not win.library_toolbar.get_visible()
        assert '自动校准中' in win.color_status.get_text()
        win.color_widgets['blue']['prefer'].emit('clicked')
        stage = 6
    elif stage == 6:
        profile = {row['key']: row for row in colors.learning_model(library)[1]}
        if profile['blue']['mode'] != 'prefer':
            return True
        win.color_widgets['orange']['avoid'].emit('clicked')
        stage = 7
    elif stage == 7:
        profile = {row['key']: row for row in colors.learning_model(library)[1]}
        if profile['orange']['mode'] != 'avoid':
            return True
        win.color_widgets['gray']['ignore'].emit('clicked')
        stage = 8
    elif stage == 8:
        profile = {row['key']: row for row in colors.learning_model(library)[1]}
        if profile['gray']['mode'] != 'ignore':
            return True
        if not capture_widget(root / 'color-preferences.png', win):
            return True
        chip = win.color_widgets['blue']['chip']
        chip.over_panel = True
        chip.popup()
        stage = 9
    elif stage == 9:
        chip = win.color_widgets['blue']['chip']
        assert chip.popover.get_visible()
        if not capture_widget(root / 'color-actions.png', chip.popover):
            return True
        chip.close_details()
        win.show_account_settings()
        win.account_username.set_text('example')
        win.account_key.set_text('ExampleTestKey')
        assert isinstance(win.account_key, Gtk.PasswordEntry)
        win.fetch_collections()
        stage = 10
    elif stage == 10:
        if not win.account_editor.get_sensitive():
            return True
        assert len(win.collection_controls) == 2
        win.collection_controls['1'].set_selected(list(accounts.MODE_LABELS).index('favorite'))
        win.collection_controls['2'].set_selected(list(accounts.MODE_LABELS).index('learn'))
        win.account_automatic.set_active(True)
        assert win.save_account_settings()
        win.account_key.set_text('')  # Documentation screenshot contains no credential.
        stage = 10.5
    elif stage == 10.5:
        if not capture_widget(root / 'account-sync.png', win.account_window):
            return True
        try:
            app.demo = False
            with patch('perch.gui.subprocess.Popen', side_effect=fake_sync):
                win.start_collection_sync()
        finally:
            app.demo = True
        stage = 11
    elif stage == 11:
        win.poll()
        if win.process or win.refresh_busy:
            return True
        item = next(row for row in win.items if row.wid == 'abcde0')
        assert item.favorite and item.liked
        assert '同步完成' in win.account_status.get_text()
        saved = accounts.load_account(accounts.account_path(root / 'config.json'))
        assert saved.automatic and saved.collections['1']['mode'] == 'favorite'
        win.account_username.set_text('another')
        assert win.save_account_settings()
        assert not win.collection_controls, 'Switching accounts must not reuse someone else’s collection choices'
        win.account_window.close()
        stage = 12
    else:
        print('GUI PASS: conditional favourite, unlike cascade, preview refresh, tone controls, masked credentials, account switching and collection worker', flush=True)
        app.quit()
        return False
    return True


GLib.timeout_add(900, check)
with patch('perch.accounts.Client', return_value=CollectionClient()):
    app.run([])
raise SystemExit(1 if errors or stage < 12 else 0)
