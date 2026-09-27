from test_core import TemporaryLibrary, FakeClient
from perch.desktop import WallpaperStateError
from perch.downloader import update, replace_wallpaper


class WallpaperProtectionTests(TemporaryLibrary):
    def seed(self):
        return [self.image(f'abcde{i}', modified=i) for i in range(5)]

    def test_current_wallpapers_are_excluded_without_changing_favorites(self):
        paths = self.seed()
        self.desktop.return_value = set(paths[:2])
        self.assertEqual([item.path for item in self.library.cleanup_candidates(2)], [paths[2]])
        self.assertEqual(self.library.prune(2), [paths[2].name])
        self.assertTrue(all(path.exists() for path in paths[:2]))
        self.assertEqual(self.library.favorite_ids(), set())

    def test_confirmation_rechecks_current_wallpaper_and_respects_reviewed_list(self):
        paths = self.seed()
        approved = {item.path.name for item in self.library.cleanup_candidates(2)}
        self.desktop.return_value = {paths[0]}
        self.library.prune(2, approved)
        self.assertTrue(paths[0].exists())
        self.assertFalse(paths[1].exists())
        self.assertFalse(paths[2].exists())
        self.assertTrue(paths[3].exists())
        self.desktop.return_value = set()
        self.assertEqual(self.library.prune(2), [paths[0].name])

    def test_rechecks_before_each_deletion_when_desktop_cycles(self):
        paths = self.seed()
        # Initial candidate list and the first deletion see no active library image;
        # desktop cycling selects the next candidate before its deletion.
        self.desktop.side_effect = [set(), set(), {paths[1]}, {paths[1]}]
        self.assertEqual(self.library.prune(2), [paths[2].name, paths[0].name])
        self.assertTrue(paths[1].exists())

    def test_failed_query_never_deletes_candidates(self):
        paths = self.seed()
        self.desktop.side_effect = WallpaperStateError('无法读取，已暂停清理')
        with self.assertRaises(WallpaperStateError):
            self.library.prune(2)
        self.assertTrue(all(path.exists() for path in paths))

    def test_query_failure_after_preview_keeps_all_unprocessed_images(self):
        paths = self.seed()
        self.desktop.side_effect = [set(), WallpaperStateError('读取失败')]
        with self.assertRaises(WallpaperStateError):
            self.library.prune(2)
        self.assertTrue(all(path.exists() for path in paths))

    def test_automatic_update_keeps_desktop_image(self):
        active = self.image('abcde0', 'red', 1)
        old = self.image('abcde1', 'green', 2)
        self.desktop.return_value = {active}
        self.config.keep = 1
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde2'])), 0)
        self.assertTrue(active.exists())
        self.assertFalse(old.exists())
        self.assertEqual(len(self.library.items()), 2)

    def test_dislike_download_keeps_active_original_and_new_image(self):
        active = self.image('abcde0', 'red')
        self.desktop.return_value = {active}
        with self.assertRaisesRegex(RuntimeError, '正被桌面使用'):
            replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1']))
        self.assertTrue(active.exists())
        self.assertTrue((self.library.directory / 'wallhaven-abcde1.png').exists())
        self.assertEqual(self.library.feedback()['abcde0'], -1)
        # Once the desktop changes, a later replacement may remove the old image.
        self.desktop.return_value = set()
        self.assertTrue(self.library.finish_replacement(self.library.directory / 'wallhaven-abcde1.png'))
        self.assertFalse(active.exists())

    def test_dislike_cleanup_rechecks_desktop_immediately_before_unlink(self):
        active = self.image('abcde0', 'red')
        new = self.image('abcde1', 'green')
        self.library.mark_disliked('abcde0')
        self.desktop.side_effect = [set(), {active}]
        self.assertFalse(self.library.finish_replacement(new, 'abcde0'))
        self.assertTrue(active.exists())

    def test_reader_receives_selected_backend(self):
        self.seed()
        self.library.prune(2, backend='gnome')
        self.assertTrue(all(call.args == ('gnome',) for call in self.desktop.call_args_list))
