from test_core import TemporaryLibrary
from test_recommendation import tags
from perch import colors
from perch.learning import FAVORITE_STRENGTH
from perch.recommendation import learning_model


class FavoriteStrengthTests(TemporaryLibrary):
    def seed(self, count=20):
        for index in range(count):
            wid = f'fav{index:03}'
            self.image(wid)
            self.library.set_liked(wid, True)
            self.library.save_tags(wid, tags(1))
            colors.save_palette(self.library, wid, ['0066cc'])

    def profiles(self):
        tag = next(row for row in self.library.tag_profile() if row['name'] == 'sky')
        color = next(row for row in colors.learning_model(self.library)[1] if row['key'] == 'blue')
        return tag, color

    def test_favorites_strengthen_tags_and_colors_without_extra_samples(self):
        self.seed()
        before = self.profiles()
        for item in self.library.items():
            self.library.set_favorite(item.wid, True)
        after = self.profiles()
        self.assertEqual(learning_model(self.library)[0], 20)
        self.assertEqual(colors.learning_model(self.library)[0], 20)
        for liked, favorite in zip(before, after):
            self.assertEqual((favorite['positive'], favorite['favorites']), (20, 20))
            self.assertAlmostEqual(favorite['positive_mass'] / liked['positive_mass'], FAVORITE_STRENGTH, places=5)
            self.assertAlmostEqual(favorite['support'], liked['support'], places=5)
            self.assertGreater(favorite['weight'], liked['weight'])
        self.assertTrue(all(sign == 1 for sign in self.library.feedback().values()))

    def test_unfavorite_restores_plain_like_and_unlike_removes_both(self):
        self.seed()
        base = self.profiles()
        self.library.set_favorite('fav000', True)
        self.assertGreater(self.profiles()[0]['weight'], base[0]['weight'])
        self.library.set_favorite('fav000', False)
        for liked, unfavorited in zip(base, self.profiles()):
            self.assertEqual(unfavorited['favorites'], 0)
            self.assertEqual(unfavorited['positive'], 20)
            self.assertAlmostEqual(unfavorited['weight'], liked['weight'], places=5)
        self.library.set_favorite('fav000', True)
        self.library.set_liked('fav000', False)
        self.assertEqual(self.profiles()[0]['positive'], 19)
        self.assertEqual(self.profiles()[0]['favorites'], 0)
        self.assertEqual(learning_model(self.library)[0], 19)

    def test_favorites_do_not_finish_calibration_early(self):
        self.seed(19)
        for item in self.library.items():
            self.library.set_favorite(item.wid, True)
        self.assertEqual(learning_model(self.library), (19, []))
        self.assertEqual(colors.learning_model(self.library)[0], 19)
        self.assertTrue(all(row['weight'] == 0 for row in colors.learning_model(self.library)[1]))

    def test_three_favorites_are_still_only_three_uncertain_observations(self):
        self.seed()
        for index in range(20):
            wid = f'fav{index:03}'
            if index < 3:
                self.library.set_favorite(wid, True)
            else:
                self.library.save_tags(wid, tags(2))
                colors.save_palette(self.library, wid, ['cc3333'])
        for row in self.profiles():
            self.assertEqual(row['positive'], 3)
            self.assertEqual(row['favorites'], 3)
            self.assertEqual(row['weight'], 0)

    def test_five_favorites_and_four_dislikes_remain_neutral(self):
        self.seed()
        for index in range(20):
            wid = f'fav{index:03}'
            if index < 5:
                self.library.set_favorite(wid, True)
            elif index < 9:
                self.library.mark_disliked(wid)
            else:
                self.library.save_tags(wid, tags(2))
                colors.save_palette(self.library, wid, ['cc3333'])
        for row in self.profiles():
            self.assertEqual((row['positive'], row['negative'], row['favorites']), (5, 4, 5))
            self.assertEqual(row['weight'], 0)

    def test_duplicate_remote_like_and_repeated_favorite_do_not_stack(self):
        self.seed()
        self.library.set_favorite('fav000', True)
        before = self.profiles()
        with self.library.connect() as db:
            db.execute("INSERT INTO remote_likes VALUES ('fav000')")
        self.library.set_favorite('fav000', True)
        for previous, current in zip(before, self.profiles()):
            self.assertEqual((current['positive'], current['favorites']), (20, 1))
            self.assertAlmostEqual(current['positive_mass'], previous['positive_mass'], places=5)

    def test_new_favorite_uses_newer_intent_date_without_rewriting_old_like(self):
        self.seed(1)
        self.library.set_favorite('fav000', True)
        with self.library.connect() as db:
            db.execute("UPDATE likes SET created='2020-01-01 00:00:00'")
            db.execute("UPDATE favorites SET created='2025-01-01 00:00:00'")
        self.assertEqual(self.library.feedback_records()['fav000'][1], 1735689600)
        self.assertEqual(self.library.feedback_records(False)['fav000'][1], 1577836800)
        self.library.set_favorite('fav000', False)
        self.assertEqual(self.library.feedback_records()['fav000'][1], 1577836800)

    def test_manual_and_neutral_overrides_still_win(self):
        self.seed()
        for item in self.library.items():
            self.library.set_favorite(item.wid, True)
        self.library.set_tag_override('sky', 'avoid')
        colors.set_override(self.library, 'blue', 'avoid')
        self.assertEqual([row['weight'] for row in self.profiles()], [-1.5, -1.5])
        self.library.set_spec_tag('sky', True)
        colors.set_override(self.library, 'blue', 'ignore')
        self.assertEqual(self.library.tag_profile(), [])
        self.assertEqual(next(row for row in colors.learning_model(self.library)[1] if row['key'] == 'blue')['weight'], 0)
