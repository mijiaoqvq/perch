"""Dislike is a one-for-one replacement, never a bulk retention action."""
import contextlib
import io
import json
import threading
import unittest
from unittest.mock import patch

from test_core import TemporaryLibrary, FakeClient
from perch.__main__ import main
from perch.config import save
from perch.downloader import replace_wallpaper, update
from perch.library import Library


class DislikeTests(TemporaryLibrary):
    def test_invalid_id_never_falls_back_to_bulk_update(self):
        self.image('abcde0')
        for wid in ('', '../abc', None):
            with self.assertRaises(ValueError):
                replace_wallpaper(self.config, self.library, wid, FakeClient(['abcde1']))
        self.assertEqual(len(self.library.items()), 1)

    def test_replace_exactly_one_without_pruning_other_files(self):
        original = self.image('abcde0', 'red', 4)
        old = self.image('abcde1', 'green', 1)
        favorite = self.image('abcde2', 'yellow', 2)
        self.library.set_favorite('abcde2', True)
        self.config.keep = 1  # Even an overfull library must lose only the chosen file.
        path = replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde3', 'abcde4']))
        self.assertTrue(path.exists())
        self.assertFalse(original.exists())
        self.assertTrue(old.exists())
        self.assertTrue(favorite.exists())
        self.assertEqual(len(self.library.items()), 3)
        self.assertFalse(self.library.seen(wid='abcde4'))
        self.assertTrue(Library(self.library.directory, self.library.state).seen(wid='abcde0'))

    def test_does_not_fill_empty_slots_or_download_a_batch(self):
        self.image('abcde0')
        self.config.keep, self.config.batch = 20, 3
        replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1', 'abcde2'], 'unique'))
        self.assertEqual(len(self.library.items()), 1)
        self.assertFalse(self.library.seen(wid='abcde2'))

    def test_network_failure_preserves_original_and_records_dislike(self):
        original = self.image('abcde0')
        data = original.read_bytes()
        with self.assertRaisesRegex(RuntimeError, '原图已保留'):
            replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1'], fail=True))
        self.assertEqual(original.read_bytes(), data)
        self.assertTrue(self.library.items()[0].disliked)
        self.assertTrue(self.library.seen(wid='abcde0'))
        self.assertEqual(list(self.library.directory.glob('*.part')), [])
        self.library.prune(0)
        self.assertTrue(original.exists())

    def test_retry_and_next_update_can_finish_failed_replacement(self):
        original = self.image('abcde0')
        with self.assertRaises(RuntimeError):
            replace_wallpaper(self.config, self.library, 'abcde0', FakeClient([]))
        self.config.keep = 1
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde1'])), 0)
        self.assertFalse(original.exists())
        self.assertEqual(len(self.library.items()), 1)

    def test_later_failed_download_cannot_reuse_one_replacement_twice(self):
        for wid, color in [('abcde0', 'red'), ('abcde1', 'green')]:
            self.image(wid, color)
            self.library.mark_disliked(wid)
        self.config.keep = self.config.batch = 2
        client = FakeClient(['abcde2', 'abcde3'])
        fetch = client.fetch
        def fail_second(url, sink, limit):
            if 'abcde3' in url:
                raise OSError('offline')
            fetch(url, sink, limit)
        client.fetch = fail_second
        self.assertEqual(update(self.config, self.library, client), 1)
        self.assertEqual(sum(item.disliked for item in self.library.items()), 1)
        self.assertEqual(len(self.library.items()), 2)

    def test_favorite_cannot_be_disliked(self):
        original = self.image('abcde0')
        self.library.set_favorite('abcde0', True)
        with self.assertRaisesRegex(ValueError, '取消收藏'):
            replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1']))
        self.assertTrue(original.exists())
        self.assertFalse(self.library.items()[0].disliked)
        self.assertEqual(len(self.library.items()), 1)

    def test_favorite_during_download_cancels_removal(self):
        original = self.image('abcde0')
        client = FakeClient(['abcde1'])
        fetch = client.fetch
        def favorite_during_fetch(url, sink, limit):
            self.library.set_favorite('abcde0', True)
            fetch(url, sink, limit)
        client.fetch = favorite_during_fetch
        with self.assertRaisesRegex(RuntimeError, '已收藏'):
            replace_wallpaper(self.config, self.library, 'abcde0', client)
        self.assertTrue(original.exists())
        self.assertIn('abcde0', self.library.favorite_ids())
        self.assertFalse(next(item for item in self.library.items() if item.wid == 'abcde0').disliked)

    def test_same_content_under_different_id_is_not_a_new_wallpaper(self):
        original = self.image('abcde0', 'red')
        with self.assertRaises(RuntimeError):
            replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1'], 'red'))
        self.assertTrue(original.exists())
        self.assertEqual(len(self.library.items()), 1)
        self.assertTrue(self.library.seen(wid='abcde1'))

    def test_corrupt_disliked_id_is_excluded_from_future_candidates(self):
        original = self.image('abcde0')
        original.write_bytes(b'corrupt')
        path = replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde0', 'abcde1']))
        self.assertEqual(path.name, 'wallhaven-abcde1.png')
        self.assertFalse(original.exists())
        self.assertTrue(self.library.seen(wid='abcde0'))

    def test_waits_for_running_updater(self):
        self.image('abcde0')
        errors, result = [], []
        started = threading.Event()
        def run():
            started.set()
            try:
                result.append(replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1'])))
            except Exception as exc:
                errors.append(exc)
        with self.library.locked('run.lock'):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assertFalse(result)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(result), 1)

    def test_cli_returns_new_path_for_preview(self):
        self.image('abcde0')
        path = self.root / 'settings.json'
        save(self.config, path)
        argv = ['perch', 'replace', '--wallpaper-id', 'abcde0', '--config', str(path),
                '--state-directory', str(self.library.state)]
        output = io.StringIO()
        with patch('sys.argv', argv), patch('perch.downloader.Client', return_value=FakeClient(['abcde1'])), \
             patch('perch.__main__.logging.basicConfig'), \
             patch('perch.__main__.RotatingFileHandler'), contextlib.redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())['path'], str(self.library.directory / 'wallhaven-abcde1.png'))

    def test_queued_images_are_protected_and_each_replaced_once(self):
        originals = [self.image('abcde0', 'red'), self.image('abcde1', 'green')]
        probe = self.image('probe0', 'yellow')
        marked = {wid: threading.Event() for wid in ('abcde0', 'abcde1')}
        original_mark = self.library.mark_disliked
        def mark(wid):
            original_mark(wid)
            marked[wid].set()
        results, errors = [], []
        def run(wid, new):
            try:
                results.append(replace_wallpaper(self.config, self.library, wid, FakeClient([new], 'unique')))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.library, 'mark_disliked', side_effect=mark), self.library.locked('run.lock'):
            workers = [threading.Thread(target=run, args=(f'abcde{i}', f'fresh{i}'), daemon=True) for i in range(2)]
            for worker in workers:
                worker.start()
            for event in marked.values():
                self.assertTrue(event.wait(2))
            self.assertTrue(all(self.library.replacement_pending(wid) for wid in marked))
            self.assertEqual(self.library.feedback(), {'abcde0': -1, 'abcde1': -1})
            # A regular updater cannot consume a queued replacement or prune its original.
            self.assertFalse(self.library.finish_replacement(probe))
            self.library.prune(1)
            self.assertTrue(all(path.exists() for path in originals))
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual({path.name for path in results}, {'wallhaven-fresh0.png', 'wallhaven-fresh1.png'})
        self.assertTrue(all(not path.exists() for path in originals))
        self.assertTrue(probe.exists())

    def test_duplicate_worker_is_rejected_without_downloading(self):
        original = self.image('abcde0')
        client = FakeClient(['fresh0'])
        with self.library.locked('replace-abcde0.lock'), patch.object(client, 'fetch') as download:
            with self.assertRaises(BlockingIOError):
                replace_wallpaper(self.config, self.library, 'abcde0', client)
            download.assert_not_called()
        self.assertTrue(original.exists())
        self.assertFalse(self.library.replacement_pending('abcde0'))
        self.assertTrue(replace_wallpaper(self.config, self.library, 'abcde0', client).exists())

    def test_new_like_while_waiting_cancels_queued_replacement(self):
        original = self.image('abcde0')
        marked = threading.Event()
        original_mark = self.library.mark_disliked
        errors = []
        client = FakeClient(['fresh0'])
        def mark(wid):
            original_mark(wid)
            marked.set()
        def run():
            try:
                replace_wallpaper(self.config, self.library, 'abcde0', client)
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.library, 'mark_disliked', side_effect=mark), self.library.locked('run.lock'), \
             patch.object(client, 'fetch') as download:
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            self.assertTrue(marked.wait(2))
            self.library.set_liked('abcde0', True)
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('反馈已修改', str(errors[0]))
        download.assert_not_called()
        self.assertTrue(original.exists())
        self.assertEqual(self.library.feedback(), {'abcde0': 1})


if __name__ == '__main__':
    unittest.main()
