from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from perch.config import Config, save
from perch import scheduler
from perch import install


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config/perch/config.json'
        self.override = self.root / 'config/systemd/user/wallhaven-anime.timer.d/perch.conf'
        installed = self.root / '.local/share/perch/perch/__init__.py'
        installed.parent.mkdir(parents=True)
        installed.write_text('old package')
        self.calls = []
        self.addCleanup(patch.stopall)
        patch('pathlib.Path.home', return_value=self.root).start()
        patch.dict('os.environ', {'XDG_CONFIG_HOME': str(self.root / 'config'),
                                  'XDG_STATE_HOME': str(self.root / 'state'),
                                  'XDG_DATA_HOME': str(self.root / 'data')}).start()

    def systemctl(self, *args):
        self.calls.append(args)
        if '--property=UnitFileState' in args:
            return 'enabled'
        if '--property=ActiveState' in args:
            return 'active'
        if '--property=LoadState' in args:
            return 'loaded'
        if '--property=Persistent' in args:
            return 'yes'
        return ''

    def test_apply_schedule(self):
        with patch.object(scheduler, 'systemctl', side_effect=self.systemctl):
            scheduler.apply(Config(active_start='09:00', active_end='21:00', interval_hours=3))
        self.assertIn('OnCalendar=*-*-* 09:00:00', self.override.read_text())
        self.assertIn(('restart', scheduler.TIMER), self.calls)
        self.assertTrue(self.config_path.exists())

    def test_disabling_stops_and_disables_timer(self):
        with patch.object(scheduler, 'systemctl', side_effect=self.systemctl):
            scheduler.apply(Config(enabled=False))
        self.assertIn(('disable', '--now', scheduler.TIMER), self.calls)

    def test_failure_restores_files_and_enablement(self):
        save(Config(keep=47), self.config_path)
        self.override.parent.mkdir(parents=True)
        self.override.write_text('old override')
        previous = self.config_path.read_bytes()
        def fail(*args):
            if args == ('restart', scheduler.TIMER):
                raise RuntimeError('restart failed')
            return self.systemctl(*args)
        with patch.object(scheduler, 'systemctl', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'restart failed'):
                scheduler.apply(Config(keep=10))
        self.assertEqual(self.config_path.read_bytes(), previous)
        self.assertEqual(self.override.read_text(), 'old override')
        self.assertIn(('enable', scheduler.TIMER), self.calls)
        self.assertIn(('start', scheduler.TIMER), self.calls)

    def test_installer_backs_up_old_files_and_reuses_units(self):
        wrapper = self.root / '.local/bin/wallhaven-anime.py'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('old worker')
        with patch.object(scheduler, 'systemctl', side_effect=self.systemctl), patch.object(install.shutil, 'which', return_value=None):
            install.main()
        self.assertIn('Compatibility entry point', wrapper.read_text())
        self.assertTrue((self.root / '.local/bin/perch').exists())
        self.assertTrue((self.root / 'data/applications/io.github.mijiaoqvq.Perch.desktop').exists())
        self.assertTrue(list((self.root / 'state/perch/backups').glob('*/manifest.json')))
        self.assertIn(('stop', scheduler.TIMER, scheduler.SERVICE), self.calls)
        self.assertIn('Persistent=true', self.override.read_text())

    def test_installer_rolls_back_on_service_failure(self):
        wrapper = self.root / '.local/bin/wallhaven-anime.py'
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text('old worker')
        failed = False
        def fail(*args):
            nonlocal failed
            if args == ('start', scheduler.TIMER) and not failed:
                failed = True
                raise RuntimeError('start failed')
            return self.systemctl(*args)
        with patch.object(scheduler, 'systemctl', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                install.main()
        self.assertEqual(wrapper.read_text(), 'old worker')
        self.assertFalse((self.root / '.local/bin/perch').exists())
        self.assertEqual((self.root / '.local/share/perch/perch/__init__.py').read_text(), 'old package')


if __name__ == '__main__':
    unittest.main()
