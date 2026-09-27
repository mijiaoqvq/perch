"""Native scroll regression checks; temporary images, fake workers, no network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch.config import Config, save
from perch.gui import Application, GLib, Gtk
from perch.library import Library

root = Path(tempfile.mkdtemp(prefix='perch-scroll-gui-'))
library = Library(root / 'images', root / 'state')
for index in range(60):
    path = library.directory / f'wallhaven-test{index:02}.png'
    Image.new('RGB', (320, 180), (40 + index * 3, 90, 110)).save(path)
    os.utime(path, (1700000000 + index, 1700000000 + index))
config = Config(directory=str(library.directory), keep=60, batch=1)
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
errors, workers = [], []
finished = False
attempts = 0


class Worker:
    returncode = None

    def __init__(self, command, **kwargs):
        self.wid = command[command.index('--wallpaper-id') + 1]
        self.output = kwargs['stdout']
        library.mark_disliked(self.wid)
        workers.append(self)

    def poll(self):
        return self.returncode

    def finish(self):
        (library.directory / f'wallhaven-{self.wid}.png').unlink()
        path = library.directory / f'wallhaven-new{workers.index(self):03}.png'
        Image.new('RGB', (320, 180), 'teal').save(path)
        self.output.write(json.dumps({'path': str(path)}).encode())
        self.output.flush()
        self.returncode = 0


def descendants(widget):
    child = widget.get_first_child()
    while child:
        yield child
        yield from descendants(child)
        child = child.get_next_sibling()


def near(actual, expected, message):
    assert abs(actual - expected) <= 2, f'{message}: {actual} != {expected}'


def log_anchor(win):
    rect = win.log_view.get_visible_rect()
    _, iterator = win.log_view.get_iter_at_location(rect.x, rect.y)
    end = iterator.copy()
    end.forward_to_line_end()
    text = win.log_view.get_buffer().get_text(iterator, end, False)
    return text, rect.y - win.log_view.get_iter_location(iterator).y


def checks():
    global finished
    win = app.window
    win.sync_tags = lambda *_: None
    adjustment = win.gallery_scroll.get_vadjustment()
    assert adjustment.get_upper() > 3000
    adjustment.set_value(1200)
    yield
    # Focus a real, visible card button before clicking, as pointer input does.
    visible = [(wid, row) for wid, row in win.gallery_rows.items()
               if adjustment.get_value() <= row.get_allocation().y < adjustment.get_value() + 220]
    assert len(visible) >= 2
    controls = [next(widget for widget in descendants(row) if isinstance(widget, Gtk.Button)
                     and widget.get_icon_name() == 'action-unavailable-symbolic') for _, row in visible[:2]]
    controls[0].grab_focus()
    yield
    before = adjustment.get_value()
    try:
        app.demo = False
        controls[0].emit('clicked')
        # A second click arrives before GTK has allocated the rebuilt cards.
        win.dislike(next(item for item in win.items if item.wid == visible[1][0]))
    finally:
        app.demo = True
    yield
    near(adjustment.get_value(), before, 'Consecutive dislikes moved the gallery')
    assert len(win.replacement_jobs) == 2
    expected = win.gallery_anchor()
    for worker in workers:
        worker.finish()
    win.poll()
    yield
    near(adjustment.get_value(), expected(), 'Completed replacements moved the surviving row')
    before = adjustment.get_value()
    item = next(item for item in win.items if item.wid == visible[2][0]) if len(visible) > 2 else win.items[20]
    win.toggle_like(item)
    yield
    near(adjustment.get_value(), before, 'Liking an image moved the gallery')
    win.render_gallery()
    win.render_gallery()
    win.refresh_library()
    yield
    near(adjustment.get_value(), before, 'Repeated/background refresh moved the gallery')
    win.order.set_selected(1)
    yield
    near(adjustment.get_value(), 0, 'Explicit sorting should start at the top')
    adjustment.set_value(3000)
    yield
    for item in library.items()[2:]:
        item.path.unlink()
    win.refresh_library()
    yield
    near(adjustment.get_value(), 0, 'Shrinking to one row must clamp the position')
    print('GUI PASS: focused dislikes, rapid refreshes, async replacements, likes, sorting and shrinking gallery', flush=True)

    log = library.state / 'perch.log'
    # Long enough to scroll, with Unicode and occasional wrapped paragraphs.
    lines = [f'{index:04} 更新记录：图片已保存，风景常新。' + ('更多内容 ' * 20 if index % 11 == 0 else '') + '\n'
             for index in range(400)]
    log.write_text(''.join(lines))
    win.nav.select_row(win.nav.get_row_at_index(4))
    yield
    adjustment = win.log_scroll.get_vadjustment()
    near(adjustment.get_value(), adjustment.get_upper() - adjustment.get_page_size(), 'Initial log should show latest entries')
    adjustment.set_value(adjustment.get_upper() * .6)
    yield
    before = adjustment.get_value()
    anchor = log_anchor(win)
    buffer = win.log_view.get_buffer()
    _, start = buffer.get_iter_at_line(250)
    end = start.copy()
    end.forward_chars(12)
    buffer.select_range(start, end)
    selected = buffer.get_text(*buffer.get_selection_bounds(), False)
    changes = []
    buffer.connect('changed', lambda *_: changes.append(True))
    win.read_logs()
    win.read_logs()
    yield
    assert not changes, 'Unchanged logs must not replace the buffer'
    near(adjustment.get_value(), before, 'Unchanged logs moved the viewport')
    with log.open('a') as stream:
        stream.write('新增记录一\n新增记录二\n')
    win.read_logs()
    yield
    near(adjustment.get_value(), before, 'Appending logs interrupted reading')
    assert log_anchor(win) == anchor
    assert buffer.get_text(*buffer.get_selection_bounds(), False) == selected, 'Appending cleared the selection'
    # Move the bounded 50 KB window forward, retaining the line being read.
    with log.open('a') as stream:
        stream.write(''.join(f'新增 {index:04} 记录，测试滚动位置。\n' for index in range(500)))
    assert log.stat().st_size > 50000
    # Force the byte boundary into a multibyte Chinese character.
    while log.read_bytes()[-50000] & 0xC0 != 0x80:
        with log.open('a') as stream:
            stream.write('x')
    win.read_logs()
    win.read_logs()
    yield
    assert log_anchor(win) == anchor, ('Moving log tail lost the visible text', log_anchor(win), anchor)
    assert buffer.get_text(*buffer.get_selection_bounds(), False) == selected, 'Moving log tail cleared retained selection'
    assert buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False) == log.read_bytes()[-50000:].decode('utf-8', errors='ignore')
    adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
    yield
    with log.open('a') as stream:
        stream.write(''.join(f'跟随新记录 {index}\n' for index in range(15)))
    win.read_logs()
    yield
    near(adjustment.get_value(), adjustment.get_upper() - adjustment.get_page_size(), 'Bottom should follow appended logs')
    adjustment.set_value(500)
    yield
    # Rotation/truncation and missing/empty files must remain usable.
    log.write_text('轮转后的新日志\n')
    win.read_logs()
    yield
    near(adjustment.get_value(), 0, 'Rotated short log must clamp to zero')
    log.unlink()
    win.read_logs()
    yield
    assert '还没有更新记录' in win.log_text
    print('GUI PASS: unchanged logs, preserved reading/selection, rolling 50 KB tail, bottom following, rotation and missing log', flush=True)
    finished = True
    app.quit()


runner = None


def check():
    global runner, attempts
    attempts += 1
    if attempts > 100:
        raise AssertionError('Scroll GUI timed out')
    win = app.window
    if not win or win.refresh_busy or win.gallery_position.target or win.log_position.target:
        return True
    if runner is None:
        runner = checks()
    try:
        next(runner)
    except StopIteration:
        return False
    return True


def fail(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()


sys.excepthook = fail
GLib.timeout_add(350, check)
with patch('perch.gui.subprocess.Popen', side_effect=Worker), \
     patch('perch.library.current_wallpapers', return_value=set()):
    app.run([])
raise SystemExit(1 if errors or not finished else 0)
