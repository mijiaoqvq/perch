from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from perch.desktop import set_wallpaper


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
