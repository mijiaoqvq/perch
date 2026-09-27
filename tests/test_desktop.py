from pathlib import Path
from types import SimpleNamespace
import tempfile
import json
import subprocess
import unittest
from unittest.mock import patch

from perch.desktop import set_wallpaper, current_wallpapers, WallpaperStateError


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'wallpaper with spaces.png'
        self.path.touch()

    def test_passes_path_as_single_argument_without_shell(self):
        result = SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch('perch.desktop.subprocess.run', return_value=result) as run:
            set_wallpaper(self.path, 'dms')
        self.assertEqual(run.call_args.args[0], ['dms', 'ipc', 'call', 'wallpaper', 'set', str(self.path)])
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_dms_text_error_is_reported_even_with_zero_exit(self):
        result = SimpleNamespace(returncode=0, stdout='ERROR: per-monitor mode is enabled', stderr='')
        with patch('perch.desktop.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'per-monitor'):
                set_wallpaper(self.path, 'dms')

    def test_backend_failure_is_reported(self):
        result = SimpleNamespace(returncode=1, stdout='', stderr='daemon is not running')
        with patch('perch.desktop.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'daemon is not running'):
                set_wallpaper(self.path, 'awww')

    def test_dms_reads_live_path_and_resolves_symlink(self):
        alias = self.path.parent / 'current wallpaper.png'
        alias.symlink_to(self.path)
        with patch('perch.desktop.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=str(alias)+'\n')) as run:
            self.assertEqual(current_wallpapers('dms'), {self.path})
        self.assertEqual(run.call_args.args[0], ['dms', 'ipc', 'call', 'wallpaper', 'get'])
        self.assertEqual(run.call_args.kwargs['timeout'], 4)
        self.assertNotIn('shell', run.call_args.kwargs)

    def test_dms_multiple_monitors_and_global_fallback_not_stale_modes(self):
        second = self.path.parent / '第二块屏幕.png'
        session = dict(perMonitorWallpaper=True, wallpaperPath=str(self.path),
                       monitorWallpapers={'DP-1': second.as_uri(), 'DP-2': '#1a1a1a'},
                       wallpaperPathLight='/old-mode.jpg', monitorWallpapersDark={'DP-1': '/old.jpg'})
        replies = [SimpleNamespace(returncode=0, stdout='ERROR: Per-monitor mode enabled. Use getFor(screenName) instead.'),
                   SimpleNamespace(returncode=0, stdout=json.dumps(session))]
        with patch('perch.desktop.subprocess.run', side_effect=replies) as run:
            self.assertEqual(current_wallpapers('dms'), {self.path, second})
            self.assertEqual(run.call_args.args[0], ['dms', 'ipc', 'call', 'settings', 'dumpSession'])

    def test_read_failure_is_not_an_empty_desktop(self):
        for response in [SimpleNamespace(returncode=1, stdout=''), SimpleNamespace(returncode=0, stdout='ERROR: unavailable'),
                         SimpleNamespace(returncode=0, stdout='unexpected response')]:
            with self.subTest(response=response), patch('perch.desktop.subprocess.run', return_value=response):
                with self.assertRaisesRegex(WallpaperStateError, '暂停清理'):
                    current_wallpapers('dms')
        with patch('perch.desktop.subprocess.run', side_effect=subprocess.TimeoutExpired('dms', 4)):
            with self.assertRaises(WallpaperStateError):
                current_wallpapers('dms')

    def test_malformed_per_monitor_state_stops_cleanup(self):
        for data in ['{}', '{', json.dumps({'perMonitorWallpaper': True, 'monitorWallpapers': [str(self.path)]})]:
            replies = [SimpleNamespace(returncode=0, stdout='ERROR: Per-monitor mode enabled.'),
                       SimpleNamespace(returncode=0, stdout=data)]
            with patch('perch.desktop.subprocess.run', side_effect=replies):
                with self.assertRaises(WallpaperStateError):
                    current_wallpapers('dms')

    def test_dms_explicitly_empty_or_solid_color_has_no_files(self):
        for value in ['', '#1a2b3c', 'we:12345']:
            with patch('perch.desktop.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=value)):
                self.assertEqual(current_wallpapers('dms'), set())

    def test_awww_and_swww_read_all_reported_outputs(self):
        output = f'DP-1: 1920x1080, scale: 1, currently displaying: image: {self.path}\n' \
                 'eDP-1: 1920x1080, scale: 1, currently displaying: image: /other image.png\n' \
                 'HDMI-1: 1920x1080, scale: 1, currently displaying: color: 000000'
        for backend in ('awww', 'swww'):
            with patch('perch.desktop.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output)):
                self.assertEqual(current_wallpapers(backend), {self.path, Path('/other image.png')})
        with patch('perch.desktop.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=output+'\nunknown output')):
            with self.assertRaises(WallpaperStateError):
                current_wallpapers('awww')

    def test_gnome_decodes_file_uris_and_preserves_light_and_dark(self):
        responses = [SimpleNamespace(returncode=0, stdout=repr(self.path.as_uri())),
                     SimpleNamespace(returncode=0, stdout="'file:///other%20wallpaper.png'")]
        with patch('perch.desktop.subprocess.run', side_effect=responses):
            self.assertEqual(current_wallpapers('gnome'), {self.path, Path('/other wallpaper.png')})

    def test_disabled_setting_backend_still_detects_and_protects(self):
        with patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': 'niri'}), \
             patch('perch.desktop.shutil.which', side_effect=lambda name: '/usr/bin/dms' if name == 'dms' else None), \
             patch('perch.desktop.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=str(self.path))):
            self.assertEqual(current_wallpapers('none'), {self.path})
        with patch.dict('os.environ', {'XDG_CURRENT_DESKTOP': ''}), patch('perch.desktop.shutil.which', return_value=None):
            with self.assertRaises(WallpaperStateError):
                current_wallpapers()
