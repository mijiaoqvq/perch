"""Native neutral/weak-evidence labels and foreground exposure integration."""
from pathlib import Path
import sys
import tempfile
import time
import traceback
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch import colors
from perch.config import Config, save
from perch.gui import Application, GLib, Gtk
from perch.library import Library
from gui_support import capture_widget

root = Path(tempfile.mkdtemp(prefix='perch-learning-gui-'))
library = Library(root / 'images', root / 'state')
for index in range(30):
    wid = f'card{index:02}'
    Image.new('RGB', (320, 180), (50 + index * 5, 110, 140)).save(library.directory / f'wallhaven-{wid}.png')
    library.save_tags(wid, [{'id': 3, 'name': 'forest'}])
for index in range(29):
    wid = f'rate{index:02}'
    positive = index < 5 if index < 9 else index < 19
    with library.connect() as db:
        db.execute(f'INSERT INTO {"likes" if positive else "dislikes"}(id) VALUES (?)', (wid,))
        if index < 2:
            db.execute('INSERT INTO favorites(id) VALUES (?)', (wid,))
    library.save_tags(wid, [{'id': 1 if index < 9 else 2, 'name': 'dress' if index < 9 else 'background'}])
    colors.save_palette(library, wid, ['0066cc' if index < 9 else '999999'])
for index in range(10):
    wid = f'weak{index:02}'
    with library.connect() as db:
        db.execute('INSERT INTO accepted VALUES (?, ?)', (wid, time.time()))
    library.save_tags(wid, [{'id': 3, 'name': 'forest'}])

config = Config(directory=str(library.directory))
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
errors = []
stage = attempts = 0
visible = set()


def label_text(widget):
    text = [widget.get_text()] if isinstance(widget, Gtk.Label) else []
    child = widget.get_first_child()
    while child:
        text.extend(label_text(child))
        child = child.get_next_sibling()
    return text


def check():
    global stage, attempts, visible
    attempts += 1
    if attempts > 50:
        raise AssertionError(f'Learning GUI timed out at {stage}')
    win = app.window
    if not win or win.refresh_busy:
        return True
    if stage == 0:
        win.nav.select_row(win.nav.get_row_at_index(2))
        stage = 1
    elif stage == 1:
        if 'dress' not in win.tag_widgets:
            return True
        chip = win.tag_widgets['dress']['chip']
        assert chip.has_css_class('tag-neutral')
        text = '\n'.join(label_text(chip.panel))
        assert '5 次正面反馈 · 4 次不喜欢' in text
        assert '其中 2 张收藏 · 喜欢强度 1.5 倍，样本不重复' in text
        assert '暂时中立' in text
        forest = win.tag_widgets['forest']['chip']
        assert forest.has_css_class('tag-neutral')
        assert '10 张展示后自然淘汰' in '\n'.join(label_text(forest.panel))
        assert win.visible_wallpapers() == set(), 'Hidden gallery must not count as exposure'
        chip.popup()
        stage = 2
    elif stage == 2:
        if not capture_widget(root / 'neutral-feedback.png', win):
            return True
        win.tag_widgets['dress']['chip'].close_details()
        win.set_likes_view('colors')
        stage = 3
    elif stage == 3:
        assert win.color_widgets['blue']['chip'].has_css_class('tag-neutral')
        assert '暂时中立' in '\n'.join(label_text(win.color_widgets['blue']['chip'].panel))
        assert '其中 2 张收藏 · 喜欢强度 1.5 倍，样本不重复' in '\n'.join(label_text(win.color_widgets['blue']['chip'].panel))
        win.nav.select_row(win.nav.get_row_at_index(0))
        win.present()
        stage = 4
    elif stage == 4:
        if not win.is_active():
            return True
        visible = win.visible_wallpapers()
        assert 0 < len(visible) < len(win.items), 'Only viewport cards should be observed'
        win.exposure_since.clear()
        win.exposure_recorded.clear()
        with patch('perch.gui.time.monotonic', return_value=100):
            win.observe_visible_wallpapers()
        with patch('perch.gui.time.monotonic', return_value=109):
            win.observe_visible_wallpapers()
        assert not win.exposure_recorded, 'Brief exposure should not count'
        with patch.object(win, 'visible_wallpapers', return_value=set()):
            win.observe_visible_wallpapers()
        assert not win.exposure_since, 'Leaving the foreground resets continuous exposure'
        with patch('perch.gui.time.monotonic', return_value=200):
            win.observe_visible_wallpapers()
        with patch('perch.gui.time.monotonic', return_value=210):
            win.observe_visible_wallpapers()
        stage = 5
    elif stage == 5:
        with library.connect() as db:
            exposed = {wid for wid, seconds in db.execute('SELECT id, seconds FROM exposures') if seconds >= 10}
        if exposed != visible:
            return True
        assert all(wid.startswith('weak') for wid in library.accepted_feedback()), 'Exposure alone is not acceptance'
        library.prune(0, natural=True)
        assert {wid for wid in library.accepted_feedback() if wid.startswith('card')} == visible
        print('GUI PASS: 5/4 neutral tag and colour, separate weak counts, viewport-only foreground dwell, natural retirement', flush=True)
        print('SCREENSHOT', root / 'neutral-feedback.png', flush=True)
        stage = 6
        app.quit()
        return False
    return True


def fail(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()


sys.excepthook = fail
GLib.timeout_add(500, check)
with patch('perch.library.current_wallpapers', return_value=set()):
    app.run([])
raise SystemExit(1 if errors or stage < 6 else 0)
