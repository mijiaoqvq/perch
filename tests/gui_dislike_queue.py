"""Native per-image dislike queue checks with independently completed fake workers."""
import json
from pathlib import Path
import sys
import tempfile
import traceback
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from perch.config import Config, save
from perch.library import Library
from perch.downloader import replace_wallpaper
from perch.gui import Application, GLib
from test_core import FakeClient

root = Path(tempfile.mkdtemp(prefix='perch-queue-gui-'))
library = Library(root / 'images', root / 'state')
for index in range(5):
    Image.new('RGB', (320, 180), (40 + 25 * index, 90, 110)).save(library.directory / f'wallhaven-test0{index}.png')
library.set_favorite('test04', True)
config = Config(directory=str(library.directory), keep=5, batch=1, min_width=32, min_height=18)
save(config, root / 'config.json')
app = Application(config, library, root / 'config.json', True)
processes, errors, messages = [], [], []
stage = attempts = 0
preview = None


class Worker:
    returncode = None

    def __init__(self, command, **kwargs):
        assert kwargs['start_new_session'] is True
        self.kind = command[3]
        self.wid = command[command.index('--wallpaper-id') + 1] if self.kind == 'replace' else None
        self.output = kwargs['stdout']
        if self.wid:
            library.mark_disliked(self.wid)
        self.new = f'fresh{len(processes)}'
        processes.append(self)

    def poll(self):
        return self.returncode

    def finish(self, success=True):
        if self.kind == 'replace':
            try:
                path = replace_wallpaper(config, library, self.wid, FakeClient([self.new] if success else [], 'unique'))
            except RuntimeError:
                self.returncode = 1
                return
            self.output.write(json.dumps({'path': str(path)}).encode())
            self.output.flush()
        self.returncode = 0


def fail(kind, value, tb):
    errors.append(str(value))
    traceback.print_exception(kind, value, tb)
    app.quit()
sys.excepthook = fail


def click(win, wid):
    item = next(item for item in win.items if item.wid == wid)
    control = win.dislike_button(item)
    assert control.get_sensitive()
    try:
        app.demo = False
        control.emit('clicked')
    finally:
        app.demo = True


def check():
    global stage, attempts, preview
    attempts += 1
    if attempts > 40:
        raise AssertionError(f'Queue GUI timed out at {stage}')
    win = app.window
    if not win or win.refresh_busy:
        return True
    if stage == 0:
        win.toast = messages.append
        click(win, 'test00')
        assert not win.dislike_button(next(i for i in win.items if i.wid == 'test00')).get_sensitive()
        click(win, 'test01')
        assert len(win.replacement_jobs) == len(processes) == 2
        try:
            app.demo = False
            win.dislike(next(i for i in win.items if i.wid == 'test00'))
        finally:
            app.demo = True
        assert len(processes) == 2, 'Rapid duplicate clicks must not spawn extra workers'
        assert not win.update_button.get_sensitive()
        assert win.dislike_button(next(i for i in win.items if i.wid == 'test02')).get_sensitive()
        assert not win.dislike_button(next(i for i in win.items if i.wid == 'test04')).get_sensitive()
        preview = win.preview(next(i for i in win.items if i.wid == 'test01'))
        assert not preview.get_child().get_sensitive()
        processes[0].finish()
        stage = 1
    elif stage == 1:
        win.poll()
        if win.refresh_busy:
            return True
        assert set(win.replacement_jobs) == {'test01'}
        assert preview.get_visible() and not preview.get_child().get_sensitive()
        assert (library.directory / 'wallhaven-fresh0.png').exists()
        assert win.dislike_button(next(i for i in win.items if i.wid == 'test02')).get_sensitive()
        processes[1].finish(False)
        stage = 2
    elif stage == 2:
        win.poll()
        if win.refresh_busy:
            return True
        assert not win.jobs_busy()
        assert preview.get_visible() and preview.get_child().get_sensitive()
        assert preview.dislike_action.get_sensitive(), 'A reopened pending preview must allow retry after failure'
        assert any('原图已保留' in text for text in messages)
        preview.close()
        click(win, 'test01')
        processes[-1].finish()
        stage = 3
    elif stage == 3:
        win.poll()
        if win.refresh_busy:
            return True
        assert not win.jobs_busy() and win.update_button.get_sensitive()
        try:
            app.demo = False
            assert win.start_job('update')
        finally:
            app.demo = True
        click(win, 'test02')
        click(win, 'test03')
        assert set(win.replacement_jobs) == {'test02', 'test03'}
        assert len(processes) == 6, 'All replacements must be submitted before the GUI closes'
        processes[3].finish()
        stage = 4
    elif stage == 4:
        win.poll()
        assert win.process is None and len(win.replacement_jobs) == 2
        assert not win.update_button.get_sensitive()
        win.close()
        # Closing the window does not kill submitted workers or discard their output.
        processes[4].finish()
        processes[5].finish()
        assert all((library.directory / f'wallhaven-fresh{i}.png').exists() for i in (0, 2, 4, 5))
        assert (library.directory / 'wallhaven-test04.png').exists()
        print('GUI PASS: consecutive dislikes, per-image locking, duplicate clicks, independent results, failure retry, reopened previews, queue during update and workers surviving close', flush=True)
        stage = 5
        app.quit()
        return False
    return True


GLib.timeout_add(900, check)
with patch('perch.gui.subprocess.Popen', side_effect=Worker), \
     patch('perch.library.current_wallpapers', return_value=set()):
    app.run([])
raise SystemExit(1 if errors or stage < 5 else 0)
