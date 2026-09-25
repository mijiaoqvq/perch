"""Optional GUI integration test. Uses isolated synthetic wallpapers; requires a display."""
from pathlib import Path
import os
import sys
import tempfile
import traceback

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch.config import Config, save, load
from perch.library import Library
from perch.gui import Application, GLib, Gtk

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
library.set_favorite('demo01', True)
library.set_favorite('demo04', True)
config = Config(directory=str(library.directory), keep=6, batch=2)
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
errors = []

def excepthook(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()
sys.excepthook = excepthook


def capture(name):
    # Use the native GTK renderer to capture only this application window.
    import ctypes
    import ctypes.util
    from gi.repository import Gsk, Graphene
    widget = app.window
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
        raise RuntimeError('Window did not produce a render node')
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

stage = 0
attempts = 0

def check():
    global stage, attempts
    attempts += 1
    if attempts > 50:
        raise AssertionError('GUI timed out')
    win = app.window
    if not win or win.refresh_busy:
        return True
    if stage == 0:
        assert len(win.items) == 9
        assert sum(i.favorite for i in win.items) == 2
        capture('gallery')
        win.toggle_favorite(next(i for i in win.items if i.wid == 'demo08'))
        stage = 1
    elif stage == 1:
        if 'demo08' not in library.favorite_ids() or sum(i.favorite for i in win.items) != 3:
            return True
        win.nav.select_row(win.nav.get_row_at_index(1))
        assert win.page_name == 'favorites'
        stage = 2
    elif stage == 2:
        capture('favorites')
        win.nav.select_row(win.nav.get_row_at_index(2))
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
        capture('settings')
        win.nav.select_row(win.nav.get_row_at_index(3))
        stage = 4
    elif stage == 4:
        capture('activity')
        win.nav.select_row(win.nav.get_row_at_index(0))
        win.search.set_text('missing')
        stage = 5
    elif stage == 5:
        assert win.gallery_stack.get_visible_child_name() == 'empty'
        win.search.set_text('')
        win.preview(win.items[0])
        stage = 6
    else:
        print('GUI PASS: gallery, favorite, navigation, settings save, schedule preview, logs, search, preview', flush=True)
        app.quit()
        return False
    return True

GLib.timeout_add(1200, check)
app.run([])
raise SystemExit(1 if errors or stage < 6 else 0)
