import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from perch.config import Config, load, save
from perch.downloader import Client, inspect_image, reconcile, update
from perch.library import Library
from perch.scheduler import timer_text


class TemporaryLibrary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = Library(self.root / 'images', self.root / 'state')
        self.config = Config(directory=str(self.library.directory), keep=3, batch=1,
                             min_width=32, min_height=18)

    def image(self, wid, color='red', modified=10):
        path = self.library.directory / f'wallhaven-{wid}.png'
        Image.new('RGB', (32, 18), color).save(path)
        os.utime(path, (modified, modified))
        return path


class LibraryTests(TemporaryLibrary):
    def test_favorites_are_extra_and_survive_cleanup(self):
        for i in range(6):
            self.image(f'abcde{i}', modified=i + 1)
        self.library.set_favorite('abcde0', True)
        removed = self.library.prune(3)
        self.assertEqual(set(removed), {'wallhaven-abcde1.png', 'wallhaven-abcde2.png'})
        self.assertEqual(len(self.library.items()), 4)
        self.assertTrue((self.library.directory / 'wallhaven-abcde0.png').exists())
        self.assertEqual(self.library.favorite_ids(), {'abcde0'})

    def test_cleanup_rechecks_favorite_after_preview(self):
        for i in range(4):
            self.image(f'abcde{i}', modified=i + 1)
        approved = {item.path.name for item in self.library.cleanup_candidates(2)}
        self.library.set_favorite('abcde0', True)
        removed = self.library.prune(2, approved)
        self.assertEqual(removed, ['wallhaven-abcde1.png'])
        self.assertTrue((self.library.directory / 'wallhaven-abcde0.png').exists())

    def test_unfavorite_does_not_immediately_delete(self):
        path = self.image('abcde0')
        self.library.set_favorite('abcde0', True)
        self.library.set_favorite('abcde0', False)
        self.assertTrue(path.exists())
        self.assertFalse(self.library.favorite_ids())

    def test_unmanaged_files_and_symlinks_are_not_deleted(self):
        original = self.root / 'personal.png'
        original.write_bytes(b'precious')
        (self.library.directory / 'wallhaven-abcde1.png').symlink_to(original)
        personal = self.library.directory / 'holiday.png'
        personal.write_bytes(b'precious')
        for i in range(2, 5):
            self.image(f'abcde{i}', modified=i)
        self.library.prune(1)
        self.assertEqual(original.read_bytes(), b'precious')
        self.assertTrue(personal.exists())
        self.assertTrue((self.library.directory / 'wallhaven-abcde1.png').is_symlink())

    def test_legacy_history_is_reused(self):
        with sqlite3.connect(self.library.state / 'history.sqlite3') as db:
            db.execute('INSERT INTO seen VALUES (?, ?)', ('abcde0', 'old-hash'))
        reopened = Library(self.library.directory, self.library.state)
        self.assertTrue(reopened.seen(wid='abcde0'))
        self.assertTrue(reopened.seen(digest='old-hash'))

    def test_invalid_favorite_is_preserved(self):
        path = self.image('abcde0')
        self.library.set_favorite('abcde0', True)
        path.write_bytes(b'corrupt')
        reconcile(self.library)
        self.library.prune(1)
        self.assertEqual(path.read_bytes(), b'corrupt')

    def test_nonblocking_update_lock(self):
        with self.library.locked('run.lock'):
            with self.assertRaises(BlockingIOError):
                with self.library.locked('run.lock', blocking=False):
                    pass


class FakeClient:
    def __init__(self, ids, color='blue', fail=False):
        self.ids, self.color, self.fail = ids, color, fail

    def candidates(self, config):
        for wid in self.ids:
            yield dict(id=wid, purity='sfw', category='anime', dimension_x=32,
                       dimension_y=18, path=f'https://w.wallhaven.cc/{wid}.png')

    def fetch(self, url, sink, limit):
        if self.fail:
            raise OSError('offline')
        wid = url.rsplit('/', 1)[1].split('.')[0]
        color = (int(wid[-2:], 36) % 255, 30, 100) if self.color == 'unique' else self.color
        Image.new('RGB', (32, 18), color).save(sink, format='PNG')


class DownloadTests(TemporaryLibrary):
    def test_first_fill_and_batch_retention(self):
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde0', 'abcde1', 'abcde2'], 'unique')), 0)
        self.assertEqual(len(self.library.items()), 3)
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde3'], 'unique')), 0)
        self.assertEqual(len(self.library.items()), 3)
        self.assertTrue(self.library.seen(wid='abcde0'))
        self.assertTrue(self.library.seen(wid='abcde3'))

    def test_download_preserves_favorites_and_fills_ordinary_slots(self):
        self.image('abcde0', 'red', 1)
        self.library.set_favorite('abcde0', True)
        result = update(self.config, self.library, FakeClient(['abcde1', 'abcde2', 'abcde3'], 'unique'))
        self.assertEqual(result, 0)
        self.assertEqual(len(self.library.items()), 4)
        self.assertTrue((self.library.directory / 'wallhaven-abcde0.png').exists())

    def test_offline_does_not_prune_existing_excess(self):
        for i in range(5):
            self.image(f'abcde{i}', modified=i)
        before = {i.path.name for i in self.library.items()}
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde9'], fail=True)), 1)
        self.assertEqual(before, {i.path.name for i in self.library.items()})

    def test_hash_dedup_and_no_download_of_known_id(self):
        self.image('abcde0', 'red')
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde0', 'abcde1'], 'red')), 1)
        self.assertEqual(len(self.library.items()), 1)
        self.assertTrue(self.library.seen(wid='abcde1'))

    def test_filter_changes_do_not_delete_old_images(self):
        path = self.image('abcde0')
        self.config.min_width = 3840
        self.config.min_height = 2160
        update(self.config, self.library, FakeClient([]))
        self.assertTrue(path.exists())

    def test_invalid_download_never_replaces_existing_library(self):
        self.image('abcde0')
        client = FakeClient(['abcde1'])
        client.fetch = lambda url, sink, limit: sink.write(b'not an image')
        self.assertEqual(update(self.config, self.library, client), 1)
        self.assertEqual(len(self.library.items()), 1)
        self.assertEqual(list(self.library.directory.glob('*.part')), [])

    def test_random_api_pagination_reuses_seed(self):
        calls = []
        def fetch(url, sink, limit):
            calls.append(url)
            sink.write(json.dumps({'data': [{'id': f'abcde{len(calls)}'}],
                                   'meta': {'last_page': 2, 'seed': 'ABC123'}}).encode())
        self.config.sorting = 'random'
        client = Client()
        client.fetch = fetch
        self.assertEqual(len(list(client.candidates(self.config))), 2)
        self.assertIn('seed=ABC123', calls[1])


class ConfigTests(unittest.TestCase):
    def test_default_schedule(self):
        self.assertEqual(Config().slots(), ['00:00', '06:00', '12:00', '18:00'])

    def test_active_window(self):
        config = Config(interval_hours=4, active_start='08:30', active_end='22:00')
        self.assertEqual(config.slots(), ['08:30', '12:30', '16:30', '20:30'])

    def test_overnight_window(self):
        config = Config(interval_hours=2, active_start='22:00', active_end='04:00')
        self.assertEqual(config.slots(), ['00:00', '02:00', '04:00', '22:00'])

    def test_fixed_times_normalize_and_override_timer(self):
        config = Config(schedule_mode='daily', daily_times=['18:30', '08:00', '08:00'])
        self.assertEqual(config.slots(), ['08:00', '18:30'])
        text = timer_text(config)
        self.assertIn('OnCalendar=\n', text)
        self.assertIn('OnCalendar=*-*-* 08:00:00', text)
        self.assertIn('Persistent=false', text)

    def test_reject_invalid_and_inconsistent_settings(self):
        for config in [Config(active_start='25:00'), Config(keep=2, batch=3), Config(keep=True),
                       Config(interval_hours=0), Config(daily_times=[]), Config(directory=''),
                       Config(ratio='shell'), Config(sorting='unknown')]:
            with self.assertRaises(ValueError):
                config.validate()

    def test_config_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'config.json'
            config = Config(keep=40, query='青空', catch_up=True)
            save(config, path)
            self.assertEqual(load(path), config)
            self.assertEqual(list(path.parent.glob('.perch-*')), [])
            path.write_text('{ invalid')
            with self.assertRaises(ValueError):
                load(path)


if __name__ == '__main__':
    unittest.main()
