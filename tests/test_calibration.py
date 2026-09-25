from dataclasses import replace

from test_core import TemporaryLibrary
from test_recommendation import TaggedClient, tags
from perch.downloader import eligible
from perch.recommendation import CALIBRATION_SAMPLES, TagFetcher, candidates, learning_model


class CalibrationTests(TemporaryLibrary):
    def rate(self, count, metadata=None, favorite=False, negative=False):
        for index in range(count):
            wid = f'cal{index:03d}'
            path = self.image(wid)
            if negative:
                self.library.mark_disliked(wid)
            else:
                self.library.set_liked(wid, True)
                if favorite:
                    self.library.set_favorite(wid, True)
            self.library.save_tags(wid, tags(1) if metadata is None else metadata)
            path.unlink()

    def discover(self):
        client = TaggedClient(['abcde0', 'abcde1'], {'abcde0': tags(2), 'abcde1': tags(1)})
        result = list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        return [item['id'] for item in result], client

    def test_early_feedback_does_not_rerank_or_search_inferred_tags(self):
        self.rate(CALIBRATION_SAMPLES - 1)
        samples, profile = learning_model(self.library)
        self.assertEqual(samples, CALIBRATION_SAMPLES - 1)
        self.assertEqual(profile, [])
        result, client = self.discover()
        self.assertEqual(result, ['abcde0', 'abcde1'])
        self.assertEqual(client.queries, [''])
        self.assertEqual(client.tag_calls, [])

    def test_threshold_enables_inference_from_deleted_history(self):
        self.rate(CALIBRATION_SAMPLES)
        self.assertEqual(self.library.items(), [])
        samples, profile = learning_model(self.library)
        self.assertEqual(samples, CALIBRATION_SAMPLES)
        self.assertEqual([tag['name'] for tag in profile], ['sky'])
        result, client = self.discover()
        self.assertEqual(result, ['abcde1', 'abcde0'])
        self.assertEqual(client.queries, ['', 'id:1'])

    def test_one_off_tag_does_not_bias_mature_profile(self):
        self.rate(CALIBRATION_SAMPLES)
        self.library.save_tags('cal000', tags(1, 2))
        self.assertEqual([tag['name'] for tag in learning_model(self.library)[1]], ['sky'])
        for wid in ('cal001', 'cal002'):
            self.library.save_tags(wid, tags(1, 2))
        self.assertEqual({tag['name'] for tag in learning_model(self.library)[1]}, {'sky', 'city'})

    def test_manual_preference_works_during_calibration(self):
        self.rate(1, negative=True)
        self.library.set_tag_override('city', 'prefer')
        samples, profile = learning_model(self.library)
        self.assertEqual(samples, 1)
        self.assertEqual([tag['name'] for tag in profile], ['city'])
        _, client = self.discover()
        self.assertEqual(client.queries, ['', 'city'])

    def test_specs_missing_tags_and_unrated_files_do_not_count(self):
        self.rate(CALIBRATION_SAMPLES, metadata=[{'id': 4, 'name': '4K'}])
        self.assertEqual(learning_model(self.library)[0], 0)
        self.library.save_tags('cal000', [])
        for index in range(25):
            self.image(f'new{index:03d}')
        self.assertEqual(learning_model(self.library)[0], 0)
        self.assertEqual(learning_model(self.library)[1], [])

    def test_like_and_favorite_are_counted_once_and_removing_feedback_recalibrates(self):
        self.rate(CALIBRATION_SAMPLES, favorite=True)
        self.assertEqual(learning_model(self.library)[0], CALIBRATION_SAMPLES)
        self.assertEqual(learning_model(self.library, False)[0], CALIBRATION_SAMPLES)
        with self.library.connect() as db:
            db.execute("DELETE FROM likes")
        self.assertEqual(learning_model(self.library, False), (0, []))
        self.library.set_tag_override('sky', 'ignore')
        self.assertEqual(learning_model(self.library)[0], 0)

    def test_tag_sync_can_complete_calibration_in_same_download(self):
        self.rate(CALIBRATION_SAMPLES)
        with self.library.connect() as db:
            db.execute("DELETE FROM wallpaper_tags WHERE wallpaper_id='cal000'")
            db.execute("DELETE FROM tag_cache WHERE id='cal000'")
        self.assertEqual(learning_model(self.library)[0], CALIBRATION_SAMPLES - 1)
        client = TaggedClient(['abcde0'], {'cal000': tags(1), 'abcde0': tags(1)})
        list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertIn('id:1', client.queries)
