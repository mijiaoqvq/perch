from dataclasses import replace

from test_core import TemporaryLibrary
from test_recommendation import TaggedClient, tags
from perch.downloader import eligible
from perch.library import Library
from perch.recommendation import TagFetcher, candidates, score_tags
from perch.tag_policy import is_spec_tag


class TagPreferenceTests(TemporaryLibrary):
    def feedback(self, wid, metadata, positive=True):
        self.image(wid)
        self.library.save_tags(wid, metadata)
        if positive:
            self.library.set_liked(wid, True)
        else:
            self.library.mark_disliked(wid)

    def discover(self, config=None, client=None):
        client = client or TaggedClient(['abcde0', 'abcde1'], {'abcde0': tags(2), 'abcde1': tags(1)})
        result = list(candidates(config or self.config, self.library, client,
                                 TagFetcher(self.library, client), eligible))
        return [item['id'] for item in result], client

    def test_manual_preference_wins_over_opposite_feedback(self):
        self.feedback('learn0', tags(1), positive=False)
        self.library.set_tag_override('sky', 'prefer')
        profile = self.library.tag_profile()
        self.assertLess(profile[0]['auto_weight'], 0)
        self.assertGreater(score_tags(tags(1), profile), 0)
        result, client = self.discover()
        self.assertEqual(result, ['abcde1', 'abcde0'])
        self.assertIn('id:1', client.queries)
        self.library.set_tag_override('sky', None)
        self.assertLess(score_tags(tags(1), self.library.tag_profile()), 0)

    def test_manual_avoid_wins_over_positive_feedback(self):
        self.feedback('learn0', tags(1))
        self.library.set_tag_override('sky', 'avoid')
        profile = self.library.tag_profile()
        self.assertGreater(profile[0]['auto_weight'], 0)
        self.assertLess(score_tags(tags(1), profile), 0)
        result, client = self.discover()
        self.assertEqual(result, ['abcde0', 'abcde1'])
        self.assertEqual(client.queries, [''])

    def test_removed_tag_stays_neutral_after_sync_feedback_and_restart(self):
        self.feedback('learn0', tags(1))
        self.library.set_tag_override('  SKY  ', 'ignore')
        self.library.save_tags('learn0', tags(1, 2))
        self.feedback('learn1', tags(1))
        reopened = Library(self.library.directory, self.library.state)
        profile = reopened.tag_profile()
        sky = next(tag for tag in profile if tag['name'] == 'sky')
        self.assertEqual(sky['mode'], 'ignore')
        self.assertEqual(sky['positive'], 2)
        self.assertEqual(score_tags(tags(1), profile), 0)
        self.assertEqual(score_tags(tags(1, 2), profile), score_tags(tags(2), profile))
        self.assertEqual(reopened.tags_for('learn1'), tags(1))
        reopened.set_tag_override('sky', None)
        self.assertGreater(score_tags(tags(1), reopened.tag_profile()), 0)

    def test_new_manual_tag_matches_by_name_then_uses_cached_id(self):
        self.library.set_tag_override('Cherry Blossoms', 'prefer')
        metadata = [{'id': 88, 'name': 'cherry blossoms'}]
        self.assertGreater(score_tags(metadata, self.library.tag_profile()), 0)
        _, client = self.discover(client=TaggedClient(['abcde0'], {'abcde0': metadata}))
        self.assertEqual(client.queries, ['', 'Cherry Blossoms'])
        _, client = self.discover()
        self.assertEqual(client.queries, ['', 'id:88'])
        self.assertEqual(len(self.library.tag_profile()), 1)

    def test_name_normalization_avoids_duplicate_weights_and_rows(self):
        self.feedback('learn0', [{'id': 1, 'name': 'ＳＫＹ'}, {'id': 2, 'name': ' Sky '}])
        self.library.set_tag_override('sky', 'prefer')
        self.library.set_tag_override(' ＳＫＹ ', 'avoid')
        profile = self.library.tag_profile()
        self.assertEqual(len(profile), 1)
        self.assertEqual(profile[0]['positive'], 1)
        self.assertEqual(profile[0]['mode'], 'avoid')

    def test_manual_adjustments_do_not_change_image_feedback_or_protection(self):
        path = self.image('learn0')
        self.library.set_favorite('learn0', True)
        self.library.set_liked('learn0', True)
        self.library.save_tags('learn0', tags(1))
        for mode in ('avoid', 'ignore', None):
            self.library.set_tag_override('sky', mode)
            self.assertEqual(self.library.feedback(), {'learn0': 1})
            self.library.prune(0)
            self.assertTrue(path.exists())
        self.assertTrue(self.library.items()[0].favorite)
        self.assertTrue(self.library.items()[0].liked)

    def test_manual_tags_respect_global_toggle_and_explicit_query(self):
        self.library.set_tag_override('sky', 'prefer')
        result, client = self.discover(replace(self.config, personalized=False))
        self.assertEqual(result, ['abcde0', 'abcde1'])
        self.assertEqual(client.queries, [''])
        self.assertEqual(client.tag_calls, [])
        _, client = self.discover(replace(self.config, query='+landscape -city'))
        self.assertEqual(client.queries, ['+landscape -city'])
        self.assertGreater(self.library.tag_profile(False)[0]['weight'], 0)

    def test_readding_ignored_tag_replaces_tombstone(self):
        self.library.set_tag_override('forest', 'ignore')
        self.library.set_tag_override('FOREST', 'prefer')
        profile = self.library.tag_profile()
        self.assertEqual(len(profile), 1)
        self.assertFalse(profile[0]['ignored'])
        self.assertGreater(score_tags(tags(3), profile), 0)

    def test_removing_all_preferences_avoids_candidate_tag_lookups(self):
        self.feedback('learn0', tags(1))
        self.library.set_tag_override('sky', 'ignore')
        result, client = self.discover()
        self.assertEqual(result, ['abcde0', 'abcde1'])
        self.assertEqual(client.queries, [''])
        self.assertEqual(client.tag_calls, [])

    def test_input_validation_leaves_existing_preferences_intact(self):
        self.library.set_tag_override('forest', 'prefer')
        for name in ('', ' ', 'a' * 201, 'sky\ncity', 'id:12', '+sky', 'sky -city', 'sky type:png'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.library.set_tag_override(name, 'prefer')
        self.assertEqual(len(self.library.tag_profile()), 1)


    def test_spec_labels_are_neutral_without_matching_subjects(self):
        for name in ('4k', '４Ｋ', '4K resolution', '4k wallpapers', '8K Ultra HD', 'Ultra HD',
                     'high resolution', 'full-hd', 'ultrawide', 'dual monitors', '1920x1080',
                     '3840 × 2160', '2560x1440 pixels', '2160p', '16:9', '21x9', 'JPEG', '超高清'):
            with self.subTest(name=name):
                self.assertTrue(is_spec_tag(name))
        for name in ('4koma', '4k anime art', 'landscape', 'portrait', 'sky', 'wide', 'dual',
                     'HD remaster art', 'high school', 'ultra instinct', 'screen girl'):
            with self.subTest(name=name):
                self.assertFalse(is_spec_tag(name))

    def test_specs_from_both_positive_and_negative_feedback_are_ignored(self):
        specs = [{'id': 4, 'name': '4k'}, {'id': 8, 'name': '8K'}, {'id': 16, 'name': '16:9'}]
        for positive in (True, False):
            with self.subTest(positive=positive):
                self.feedback('learn0', specs, positive)
                self.assertEqual(self.library.tag_profile(), [])
                visible = self.library.tag_profile(include_specs=True)
                self.assertEqual(len(visible), 3)
                self.assertTrue(all(tag['weight'] == 0 and tag['ignored'] for tag in visible))
                self.assertCountEqual(self.library.tags_for('learn0'), specs)
                self.assertEqual(score_tags(specs, visible), 0)
                _, client = self.discover()
                self.assertEqual(client.queries, [''])
                self.assertEqual(client.tag_calls, [])

    def test_specs_never_change_a_positive_or_negative_candidate_score(self):
        self.feedback('learn0', tags(1))
        self.feedback('learn1', tags(2), positive=False)
        specs = [{'id': 4, 'name': '4k'}, {'id': 16, 'name': '16:9'}]
        profile = self.library.tag_profile()
        for content in (tags(1), tags(2), tags(1, 2), []):
            self.assertEqual(score_tags(content, profile), score_tags(content + specs, profile))

    def test_specs_cannot_be_manually_recommended_or_penalized(self):
        for name in ('4K', '3840x2160', 'Ultra HD', '16:9'):
            for mode in ('prefer', 'avoid', 'ignore'):
                with self.subTest(name=name, mode=mode), self.assertRaisesRegex(ValueError, '始终保持中立'):
                    self.library.set_tag_override(name, mode)
        self.assertEqual(self.library.tag_profile(include_specs=True), [])

    def test_legacy_spec_override_is_neutral_even_if_policy_expands(self):
        # A future neutral rule must also win over already persisted adjustments.
        with self.library.connect() as db:
            db.execute("INSERT INTO tag_overrides VALUES ('4k', '4K', 'prefer')")
        self.assertEqual(self.library.tag_profile(), [])
        self.assertEqual(self.library.tag_profile(include_specs=True)[0]['weight'], 0)
        _, client = self.discover()
        self.assertEqual(client.queries, [''])
        self.library.set_tag_override('4k', None)
        self.assertEqual(self.library.tag_profile(include_specs=True), [])

    def test_custom_spec_is_neutral_in_learning_search_and_score_divisor(self):
        self.feedback('learn0', tags(1, 2))
        self.library.set_spec_tag('CITY', True)
        self.library.set_spec_tag('custom display spec', True)
        policy = self.library.spec_policy()
        profile = self.library.tag_profile()
        self.assertEqual([tag['name'] for tag in profile], ['sky'])
        more = tags(1, 2) + [{'id': 17, 'name': 'custom display spec'}]
        self.assertEqual(score_tags(more, profile, policy), score_tags(tags(1), profile, policy))
        _, client = self.discover()
        self.assertEqual(client.queries, ['', 'id:1'])
        with self.assertRaisesRegex(ValueError, '始终保持中立'):
            self.library.set_tag_override('city', 'prefer')

    def test_removed_default_spec_stays_removed_after_restart_and_sync(self):
        spec = [{'id': 4, 'name': '4K'}]
        self.feedback('learn0', spec)
        self.library.set_spec_tag('４Ｋ', False)
        self.library.save_tags('learn0', spec)
        self.library = Library(self.library.directory, self.library.state)
        self.assertFalse(self.library.spec_policy()('4k'))
        self.assertNotIn('4k', {tag['key'] for tag in self.library.spec_entries()})
        self.library.set_tag_override('4k', 'prefer')
        self.assertGreater(score_tags(spec, self.library.tag_profile(), self.library.spec_policy()), 0)
        _, client = self.discover()
        self.assertIn('id:4', client.queries)
        self.library.set_spec_tag('4K', True)
        self.assertTrue(self.library.spec_policy()('4k'))
        self.assertEqual(self.library.tag_profile(), [])

    def test_custom_spec_removal_restores_existing_preferences(self):
        self.library.set_tag_override('sky', 'avoid')
        self.library.set_spec_tag('sky', True)
        self.assertEqual(self.library.tag_profile(), [])
        self.library.set_spec_tag('sky', False)
        self.assertEqual(self.library.tag_profile()[0]['mode'], 'avoid')
        self.assertLess(self.library.tag_profile()[0]['weight'], 0)

    def test_spec_list_defaults_auto_discovery_and_normalization(self):
        before = self.library.spec_entries()
        self.assertIn('4k', {tag['key'] for tag in before})
        self.library.save_tags('learn0', [{'id': 10, 'name': '5120x2880'}])
        self.library.set_spec_tag(' ＨＤＲ ', True)
        self.library.set_spec_tag('hdr', True)
        after = self.library.spec_entries()
        self.assertEqual(len(after), len(before) + 2)
        self.assertIn('5120x2880', {tag['key'] for tag in after})
        self.library.set_spec_tag('5120x2880', False)
        self.assertFalse(self.library.spec_policy()('5120x2880'))
        with self.assertRaises(ValueError):
            self.library.set_spec_tag('', True)
