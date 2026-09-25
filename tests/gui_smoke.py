"""Optional GUI integration test. Uses isolated synthetic wallpapers; requires a display."""
from pathlib import Path
from dataclasses import replace
import json
import os
import sys
import tempfile
import traceback
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch.config import Config, save, load
from perch.library import Library
from perch.gui import Application, GLib, Gtk
from perch.downloader import replace_wallpaper
from test_core import FakeClient

root = Path(tempfile.mkdtemp(prefix='perch-gui-'))
library = Library(root / 'images', root / 'state')
colors = [('mist', '#798f91', '#bfd3c5', '#3e6357'), ('dawn', '#ca9c80', '#eddbc0', '#917657'),
          ('blue', '#516a93', '#c0cedd', '#2b496b'), ('pine', '#587766', '#abbca0', '#294b3e'),
          ('rose', '#ac818c', '#e4c2b8', '#695676'), ('dusk', '#726f99', '#d5c6d7', '#444a6e'),
          ('lake', '#619798', '#d2e0cc', '#34777b'), ('sand', '#b29e7d', '#eddfbd', '#7b8261'),
          ('mint', '#87a995', '#e0e7cf', '#4d7663')]
for index, (_, sky, sun, hill) in enumerate(colors):
    im = Image.new('RGB', (960, 540), sky)
    draw = ImageDraw.Draw(im)
    draw.ellipse((680, 60, 805, 185), fill=sun)
    draw.polygon([(0, 380), (180, 170), (420, 395), (600, 260), (960, 400), (960, 540), (0, 540)], fill=hill)
    draw.polygon([(0, 475), (360, 320), (540, 420), (790, 315), (960, 480), (960, 540), (0, 540)], fill='#263e3b')
    path = library.directory / f'wallhaven-demo0{index}.png'
    im.save(path)
    os.utime(path, (1000 + index, 1000 + index))
    library.save_tags(f'demo0{index}', [{'id': index % 3 + 1, 'name': ['sky', 'forest', 'landscape'][index % 3]},
                                     {'id': 4, 'name': '4K'}])
library.set_favorite('demo01', True)
library.set_favorite('demo04', True)
library.set_liked('demo02', True)
config = Config(directory=str(library.directory), keep=6, batch=2)
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
errors = []

def excepthook(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()
sys.excepthook = excepthook


def capture(name, widget=None):
    # Use the native GTK renderer to capture only this application window.
    import ctypes
    import ctypes.util
    from gi.repository import Gsk, Graphene
    widget = widget or app.window
    snapshot = Gtk.Snapshot()
    paintable = Gtk.WidgetPaintable.new(widget)
    paintable.snapshot(snapshot, widget.get_width(), widget.get_height())
    lib = ctypes.CDLL(ctypes.util.find_library('gtk-4'))
    capsule = ctypes.pythonapi.PyCapsule_GetPointer
    capsule.argtypes = [ctypes.py_object, ctypes.c_char_p]
    capsule.restype = ctypes.c_void_p
    ptr = lambda obj: capsule(obj.__gpointer__, None)
    lib.gtk_snapshot_to_node.argtypes = [ctypes.c_void_p]
    lib.gtk_snapshot_to_node.restype = ctypes.c_void_p
    node = lib.gtk_snapshot_to_node(ptr(snapshot))
    if not node:
        widget.queue_draw()
        widget.present()
        return False
    renderer = widget.get_native().get_renderer()
    lib.gsk_renderer_render_texture.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.gsk_renderer_render_texture.restype = ctypes.c_void_p
    texture = lib.gsk_renderer_render_texture(ptr(renderer), node, None)
    lib.gdk_texture_save_to_png.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.gdk_texture_save_to_png.restype = ctypes.c_bool
    destination = root / f'{name}.png'
    assert lib.gdk_texture_save_to_png(texture, str(destination).encode())
    lib.gsk_render_node_unref.argtypes = [ctypes.c_void_p]
    lib.gsk_render_node_unref(node)
    lib.g_object_unref.argtypes = [ctypes.c_void_p]
    lib.g_object_unref(texture)
    print('SCREENSHOT', destination, flush=True)
    return True

stage = 0
attempts = 0
preview = None
disliked_id = None


def navigate(name):
    row = app.window.nav.get_first_child()
    while row:
        if row.page == name:
            app.window.nav.select_row(row)
            return
        row = row.get_next_sibling()
    raise AssertionError(f'Navigation not found: {name}')


def fake_download(command, **kwargs):
    assert command[3] == 'replace'
    wid = command[command.index('--wallpaper-id') + 1]
    candidates = ['next01'] if stage == 5 else ['next02'] if stage == 8 else []
    try:
        path = replace_wallpaper(replace(app.window.config, min_width=32, min_height=18),
                                 library, wid, FakeClient(candidates, 'unique'))
    except RuntimeError:
        code = 1
    else:
        kwargs['stdout'].write(json.dumps({'path': str(path)}).encode())
        kwargs['stdout'].flush()
        code = 0
    return SimpleNamespace(returncode=code, poll=lambda: code)

def check():
    global stage, attempts, preview, disliked_id
    attempts += 1
    if attempts > 75:
        raise AssertionError('GUI timed out')
    win = app.window
    if not win or win.refresh_busy:
        return True
    if stage == 0:
        assert len(win.items) == 9
        assert win.gallery_hero.get_visible()
        assert sum(i.favorite for i in win.items) == 2
        if not capture('gallery'):
            return True
        win.toggle_favorite(next(i for i in win.items if i.wid == 'demo08'))
        stage = 1
    elif stage == 1:
        if 'demo08' not in library.favorite_ids() or sum(i.favorite for i in win.items) != 3:
            return True
        navigate('favorites')
        assert win.page_name == 'favorites'
        assert not win.gallery_hero.get_visible()
        stage = 2
    elif stage == 2:
        if not capture('favorites'):
            return True
        navigate('settings')
        win.controls['active_start'].set_text('08:00')
        win.controls['active_end'].set_text('23:00')
        win.controls['interval_hours'].set_value(4)
        win.controls['keep'].set_value(8)
        win.save_settings()
        stage = 3
    elif stage == 3:
        if load(root / 'config.json').keep != 8:
            return True
        assert load(root / 'config.json').slots() == ['08:00', '12:00', '16:00', '20:00']
        if not capture('settings'):
            return True
        navigate('activity')
        stage = 4
    elif stage == 4:
        if not capture('activity'):
            return True
        navigate('library')
        win.search.set_text('missing')
        stage = 5
    elif stage == 5:
        assert win.gallery_stack.get_visible_child_name() == 'empty'
        win.search.set_text('')
        item = next(i for i in win.items if not i.favorite)
        disliked_id = item.wid
        favorite = next(i for i in win.items if i.favorite)
        assert not win.dislike_button(favorite).get_sensitive()
        preview = win.preview(item)
        # Exercise the real button and worker result handling using local fixtures.
        app.demo = False
        try:
            with patch('perch.gui.subprocess.Popen', side_effect=fake_download):
                win.dislike_button(item, preview).emit('clicked')
        finally:
            app.demo = True
        assert not preview.get_child().get_sensitive()
        stage = 6
    elif stage == 6:
        win.poll()
        if win.process or win.refresh_busy:
            return True
        assert not any(i.wid == disliked_id for i in win.items)
        assert len(win.items) == 9
        assert not preview.get_visible()
        windows = [w for w in Gtk.Window.list_toplevels() if w.get_visible() and w.get_title() == 'NEXT01 · 栖景']
        assert len(windows) == 1, 'Preview should automatically show the new wallpaper'
        preview = windows[0]
        item = next(i for i in win.items if i.wid == 'next01')
        app.demo = False
        try:
            with patch('perch.gui.subprocess.Popen', side_effect=fake_download):
                win.dislike_button(item, preview).emit('clicked')
        finally:
            app.demo = True
        stage = 7
    elif stage == 7:
        win.poll()
        if win.process or win.refresh_busy:
            return True
        assert preview.get_visible() and preview.get_child().get_sensitive()
        assert next(i for i in win.items if i.wid == 'next01').disliked
        assert len(win.items) == 9
        preview.close()
        stage = 8
    elif stage == 8:
        item = next(i for i in win.items if not i.favorite and not i.disliked)
        preview = win.preview(item)
        app.demo = False
        try:
            with patch('perch.gui.subprocess.Popen', side_effect=fake_download):
                win.dislike_button(item, preview).emit('clicked')
        finally:
            app.demo = True
        preview.close()
        stage = 9
    elif stage == 9:
        win.poll()
        if win.process or win.refresh_busy:
            return True
        assert any(i.wid == 'next02' for i in win.items)
        assert not preview.get_visible()
        assert not any(w.get_visible() and w.get_title() == 'NEXT02 · 栖景' for w in Gtk.Window.list_toplevels())
        stage = 10
    elif stage == 10:
        item = next(i for i in win.items if i.wid == 'demo00')
        win.like_button(item).emit('clicked')
        stage = 11
    elif stage == 11:
        item = next(i for i in win.items if i.wid == 'demo00')
        if not item.liked:
            return True
        assert not item.favorite
        navigate('likes')
        assert win.page_name == 'likes'
        assert not win.gallery_hero.get_visible()
        assert win.gallery_stack.get_visible_child_name() == 'tags'
        win.likes_tab_buttons['images'].emit('clicked')
        assert win.gallery_stack.get_visible_child_name() == 'images'
        assert not win.gallery_hero.get_visible()
        stage = 12
    elif stage == 12:
        if not capture('likes'):
            return True
        item = next(i for i in win.items if i.wid == 'demo01')
        win.like_button(item).emit('clicked')
        stage = 13
    elif stage == 13:
        item = next(i for i in win.items if i.wid == 'demo01')
        if not item.liked:
            return True
        assert item.favorite
        win.like_button(item).emit('clicked')
        stage = 14
    elif stage == 14:
        item = next(i for i in win.items if i.wid == 'demo01')
        if item.liked:
            return True
        assert item.favorite
        navigate('settings')
        win.controls['personalized'].set_active(False)
        win.controls['favorites_influence'].set_active(False)
        win.save_settings()
        stage = 15
    elif stage == 15:
        saved = load(root / 'config.json')
        if saved.personalized:
            return True
        assert not saved.favorites_influence
        assert 'sky' in win.tag_widgets
        win.show_tag_manager()
        assert win.page_name == 'likes'
        assert win.gallery_stack.get_visible_child_name() == 'tags'
        stage = 16
    elif stage == 16:
        assert '个性化已关闭' in win.profile_status.get_text()
        assert '4K' in win.neutral_label.get_text()
        assert '4k' not in win.tag_widgets
        win.tag_entry.set_text('cherry blossoms')
        win.tag_add_button.emit('clicked')
        stage = 17
    elif stage == 17:
        if win.tag_edit_busy or 'cherry blossoms' not in win.tag_widgets:
            return True
        assert win.tag_entry.get_text() == ''
        win.tag_widgets['sky']['avoid'].emit('clicked')
        stage = 18
    elif stage == 18:
        if win.tag_edit_busy or not win.tag_widgets['sky']['avoid'].has_css_class('suggested-action'):
            return True
        assert next(t for t in library.tag_profile() if t['key'] == 'sky')['mode'] == 'avoid'
        win.tag_widgets['sky']['remove'].emit('clicked')
        stage = 19
    elif stage == 19:
        if win.tag_edit_busy or 'restore' not in win.tag_widgets['sky']:
            return True
        assert next(t for t in library.tag_profile() if t['key'] == 'sky')['weight'] == 0
        win.tag_widgets['sky']['chip'].open_details()
        if hasattr(win.toast_overlay, 'dismiss_all'):
            win.toast_overlay.dismiss_all()
        stage = 20
    elif stage == 20:
        if not capture('personalization'):
            return True
        win.tag_widgets['sky']['restore'].emit('clicked')
        stage = 21
    elif stage == 21:
        if win.tag_edit_busy or 'auto' not in win.tag_widgets['sky']:
            return True
        assert next(t for t in library.tag_profile() if t['key'] == 'sky')['mode'] == 'auto'
        win.tag_entry.set_text('4k')
        win.tag_add_button.emit('clicked')
        stage = 22
    elif stage == 22:
        if win.tag_edit_busy:
            return True
        assert win.tag_entry.get_text() == '4k'
        assert '4k' not in win.tag_widgets
        # Turn personalization back on for the representative README screenshot.
        win.tag_entry.set_text('')
        navigate('settings')
        win.controls['personalized'].set_active(True)
        win.save_settings()
        stage = 23
    elif stage == 23:
        if not load(root / 'config.json').personalized:
            return True
        win.show_tag_manager()
        if hasattr(win.toast_overlay, 'dismiss_all'):
            win.toast_overlay.dismiss_all()
        stage = 24
    elif stage == 24:
        if not capture('tag-preferences'):
            return True
        assert library.favorite_ids() == {'demo01', 'demo04', 'demo08'}
        # Exercise the motion-controller path: hover opens the card after a delay.
        win.tag_widgets['sky']['chip'].motion.emit('enter', 10.0, 10.0)
        stage = 25
    elif stage == 25:
        chip = win.tag_widgets['sky']['chip']
        assert chip.popover.get_visible()
        chip.motion.emit('leave')
        chip.panel_motion.emit('enter', 10.0, 10.0)
        stage = 26
    elif stage == 26:
        chip = win.tag_widgets['sky']['chip']
        assert chip.popover.get_visible(), 'Card must stay open when crossing into its actions'
        if not capture('tag-hover'):
            return True
        if not capture('tag-actions', chip.popover):
            return True
        chip.panel_motion.emit('leave')
        stage = 27
    elif stage == 27:
        assert not win.tag_widgets['sky']['chip'].popover.get_visible()
        win.show_spec_settings()
        stage = 28
    elif stage == 28:
        if not getattr(win, 'spec_widgets', None):
            return True
        assert '4k' in win.spec_widgets
        win.spec_entry.set_text('sky')
        win.spec_add_button.emit('clicked')
        stage = 29
    elif stage == 29:
        if win.spec_busy or 'sky' not in win.spec_widgets or 'sky' in win.tag_widgets:
            return True
        assert library.spec_policy()('sky')
        win.spec_widgets['sky']['remove'].emit('clicked')
        stage = 30
    elif stage == 30:
        if win.spec_busy or 'sky' in win.spec_widgets or 'sky' not in win.tag_widgets:
            return True
        win.spec_widgets['4k']['remove'].emit('clicked')
        stage = 31
    elif stage == 31:
        if win.spec_busy or '4k' in win.spec_widgets or '4k' not in win.tag_widgets:
            return True
        assert not library.spec_policy()('4K')
        win.spec_entry.set_text('4K')
        win.spec_add_button.emit('clicked')
        stage = 32
    elif stage == 32:
        if win.spec_busy or '4k' not in win.spec_widgets or '4k' in win.tag_widgets:
            return True
        if not capture('spec-settings', win.spec_window):
            return True
        win.spec_window.close()
        # Local Gtk.Settings changes affect only this isolated test application.
        win.original_test_font = Gtk.Settings.get_default().get_property('gtk-font-name')
        Gtk.Settings.get_default().set_property('gtk-font-name', 'Serif 13')
        stage = 33
    elif stage == 33:
        assert win.profile_label.get_pango_context().get_font_description().get_family() == 'Serif'
        assert win.log_view.get_pango_context().get_font_description().get_family() == 'Serif'
        Gtk.Settings.get_default().set_property('gtk-font-name', win.original_test_font)
        win.tag_widgets['sky']['chip'].popup()  # Keyboard/click path.
        stage = 34
    elif stage == 34:
        assert win.tag_widgets['sky']['chip'].popover.get_visible()
        navigate('library')
        assert not win.tag_widgets['sky']['chip'].popover.get_visible()
        assert win.gallery_hero.get_visible()
        stage = 35
    else:
        print('GUI PASS: gallery-only summary, hashtag hover/click cards, manual preferences, editable spec list, live system font, favorite/like protection, replacement and recovery', flush=True)
        app.quit()
        return False
    return True

GLib.timeout_add(1200, check)
app.run([])
raise SystemExit(1 if errors or stage < 35 else 0)
