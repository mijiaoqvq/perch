import time
from unittest.mock import patch

from PIL import Image

from test_core import TemporaryLibrary, FakeClient
from test_recommendation import TaggedClient, tags
from perch import colors
from perch.downloader import update, replace_wallpaper, eligible
from perch.learning import direction, IMPLICIT_MAX_BOOST
from perch.library import Library
from perch.recommendation import candidates, TagFetcher, score_tags, choose, content_features


class EvidenceTests(TemporaryLibrary):
    def rate(self, positive, negative, feature='dress', start=0, palette=None):
        for index in range(positive + negative):
            wid = f'r{start + index:05}'
            with self.library.connect() as db:
                table = 'likes' if index < positive else 'dislikes'
                db.execute(f'INSERT INTO {table}(id) VALUES (?)', (wid,))
            self.library.save_tags(wid, [{'id': start + 1, 'name': feature}])
            if palette:
                colors.save_palette(self.library, wid, palette)

    def calibrated(self):
        self.rate(10, 10, 'background', 100, ['999999'])

    def passive(self, count, feature='dress', start=1000, created=None):
        for index in range(count):
            wid = f'p{start + index:05}'
            with self.library.connect() as db:
                db.execute('INSERT INTO accepted VALUES (?, ?)', (wid, created or time.time()))
            self.library.save_tags(wid, [{'id': 1, 'name': feature}])
            colors.save_palette(self.library, wid, ['0066cc'])

    def target(self):
        return next(row for row in self.library.tag_profile() if row['name'] == 'dress')

    def test_screenshot_and_its_inverse_are_neutral_for_tags_and_colors(self):
        self.calibrated()
        self.rate(5, 4, palette=['0066cc'])
        row = self.target()
        self.assertEqual((row['positive'], row['negative'], row['weight']), (5, 4, 0))
        blue = next(row for row in colors.learning_model(self.library)[1] if row['key'] == 'blue')
        self.assertEqual(blue['weight'], 0)
        for positive, negative in [(4, 5), (50, 40), (40, 50), (1, 0), (0, 1), (3, 0), (10, 10)]:
            self.assertEqual(direction(positive, negative)[0], 0)

    def test_symmetric_confidence_requires_a_clear_direction(self):
        for positive, negative in [(4, 0), (10, 2), (20, 4), (100, 5)]:
            self.assertGreater(direction(positive, negative)[0], 0)
            self.assertAlmostEqual(direction(positive, negative)[0], -direction(negative, positive)[0])

    def test_common_background_feature_is_not_automatically_blamed(self):
        self.rate(2, 18)
        self.rate(4, 36, 'other', 100)
        self.assertEqual(self.target()['weight'], 0)
        self.assertIn('没有明确偏好差异', self.target()['reason'])

    def test_weak_feedback_never_calibrates_or_counts_as_likes(self):
        self.passive(100)
        self.assertEqual(self.library.learning_sample_count(), 0)
        self.assertEqual(colors.learning_model(self.library)[0], 0)
        self.assertEqual(self.target()['positive'], 0)
        self.assertEqual(self.target()['weight'], 0)
        self.assertEqual(self.target()['implicit_weight'], 0)
        self.assertEqual(self.library.feedback(), {})

    def test_weak_boost_is_capped_and_never_starts_tag_or_color_searches(self):
        self.calibrated()
        self.passive(100)
        row = self.target()
        self.assertEqual(row['weight'], 0)
        self.assertGreater(row['implicit_weight'], 0)
        self.assertLessEqual(row['implicit_weight'], IMPLICIT_MAX_BOOST)
        client = TaggedClient(['abcde0', 'abcde1'], {'abcde0': tags(2), 'abcde1': [{'id': 1, 'name': 'dress'}]})
        list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual(client.queries, [''])

    def test_weak_counts_cannot_flip_conflict_or_negative_evidence(self):
        self.calibrated()
        self.rate(5, 4)
        self.passive(200)
        self.assertEqual(self.target()['weight'], 0)
        self.assertEqual(self.target()['implicit_weight'], 0)
        with self.library.connect() as db:
            db.execute("DELETE FROM likes WHERE id < 'r00100'")
        self.assertLess(self.target()['weight'], 0)
        self.assertEqual(self.target()['implicit_weight'], 0)

    def test_manual_and_spec_overrides_suppress_weak_feedback(self):
        self.calibrated()
        self.passive(30)
        for mode, weight in [('prefer', 1.), ('avoid', -1.5), ('ignore', 0.)]:
            self.library.set_tag_override('dress', mode)
            self.assertEqual(self.target()['weight'], weight)
            self.assertEqual(self.target()['implicit_weight'], 0)
        self.library.set_tag_override('dress', None)
        self.library.set_spec_tag('dress', True)
        row = next(row for row in self.library.tag_profile(include_specs=True) if row['name'] == 'dress')
        self.assertEqual(row['weight'], 0)
        self.assertEqual(row['implicit_weight'], 0)

    def test_old_evidence_fades_but_manual_preferences_do_not(self):
        self.rate(20, 0)
        fresh = self.target()['weight']
        with self.library.connect() as db:
            db.execute("UPDATE likes SET created=datetime('now', '-360 days')")
        self.assertLess(self.target()['weight'], fresh)
        self.assertEqual(self.target()['weight'], 0)
        self.library.set_tag_override('dress', 'prefer')
        self.assertEqual(self.target()['weight'], 1)

    def test_old_weak_feedback_fades(self):
        self.calibrated()
        self.passive(10, created=time.time() - 300 * 86400)
        self.assertLess(self.target()['implicit_weight'], .001)

    def test_neutral_and_correlated_tags_cannot_distort_scores(self):
        profile = [dict(name='sky', weight=.5), dict(name='blue sky', weight=.5), dict(name='city', weight=-.8)]
        self.assertEqual(score_tags(tags(1), profile), score_tags(tags(1, 3), profile))
        self.assertEqual(score_tags(tags(1), profile), score_tags(tags(1) + [{'name': 'blue sky'}], profile))
        self.assertAlmostEqual(score_tags(tags(1, 2), profile), -.3)
        profile.append(dict(name='forest', weight=.1))
        self.assertEqual(score_tags(tags(1, 3), profile), .5)

    def test_weak_signal_on_another_tag_cannot_rescue_a_negative_candidate(self):
        profile = [dict(name='sky', weight=-.01), dict(name='city', weight=0, implicit_weight=.04)]
        pool = [({'id': 'mixed0'}, tags(1, 2)), ({'id': 'other0'}, tags(3))]
        index, _ = choose(pool, profile, 0, lambda _: False, [], [])
        self.assertEqual(index, 1)

    def test_recent_content_similarity_breaks_close_relevance_ties(self):
        profile = [dict(name='sky', weight=1), dict(name='city', weight=.95)]
        pool = [({'id': 'sky000'}, tags(1)), ({'id': 'city00'}, tags(2))]
        recent = [content_features({}, tags(1))]
        index, explore = choose(pool, profile, 0, lambda _: False, [], recent)
        self.assertFalse(explore)
        self.assertEqual(index, 1)

    def test_bad_optional_palette_does_not_discard_valid_tags_or_abort_ranking(self):
        self.library.set_tag_override('sky', 'prefer')
        client = TaggedClient(['abcde0'])
        client.detail = lambda wid: dict(id=wid, tags=tags(1), colors='invalid optional palette')
        original = client.candidates
        def source(config):
            yield from (dict(item, colors='invalid palette') for item in original(config))
        client.candidates = source
        result = list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual([item['id'] for item in result], ['abcde0'])
        self.assertEqual(self.library.tags_for('abcde0'), tags(1))
        self.assertEqual(colors.palette(self.library, 'abcde0'), [])


class AcceptanceLifecycleTests(TemporaryLibrary):
    def test_only_seen_unrated_natural_retirements_get_one_weak_signal(self):
        for index in range(4):
            self.image(f'abcde{index}', modified=index + 1)
        self.library.observe(['abcde0'])
        self.library.observe(['abcde1'], seconds=9)
        self.library.observe(['abcde2'])
        self.library.set_liked('abcde2', True)
        self.library.prune(0, natural=True)
        self.assertEqual(set(self.library.accepted_feedback()), {'abcde0'})
        timestamp = self.library.accepted_feedback()['abcde0']
        self.image('abcde0')
        self.library.prune(0, natural=True)
        self.assertEqual(self.library.accepted_feedback()['abcde0'], timestamp)

    def test_manual_cleanup_cancelled_likes_and_unlink_failure_do_not_reward(self):
        self.image('abcde0')
        self.library.observe(['abcde0'])
        self.library.prune(0)
        self.assertEqual(self.library.accepted_feedback(), {})
        path = self.image('abcde0')
        with patch.object(type(path), 'unlink', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):
                self.library.prune(0, natural=True)
        self.assertEqual(self.library.accepted_feedback(), {})
        self.library.set_liked('abcde0', True)
        self.library.set_liked('abcde0', False)
        self.library.prune(0, natural=True)
        self.assertEqual(self.library.accepted_feedback(), {})

    def test_current_desktop_is_observed_but_never_rewarded_or_deleted_while_active(self):
        old = self.image('abcde0', modified=1)
        self.image('abcde1', modified=2)
        self.desktop.return_value = {old}
        self.library.prune(1, natural=True)
        self.assertTrue(old.exists())
        self.assertEqual(self.library.accepted_feedback(), {})
        self.desktop.return_value = set()
        self.library.prune(1, natural=True)
        self.assertEqual(set(self.library.accepted_feedback()), {'abcde0'})

    def test_dislike_replacement_never_adds_acceptance(self):
        self.image('abcde0')
        self.library.observe(['abcde0'])
        replace_wallpaper(self.config, self.library, 'abcde0', FakeClient(['abcde1']))
        self.assertEqual(self.library.accepted_feedback(), {})
        self.assertEqual(self.library.feedback()['abcde0'], -1)

    def test_explicit_feedback_supersedes_acceptance_without_double_counting(self):
        self.image('abcde0')
        self.library.observe(['abcde0'])
        self.library.save_tags('abcde0', tags(1))
        self.library.prune(0, natural=True)
        self.image('abcde0')
        self.library.set_liked('abcde0', True)
        row = self.library.tag_profile()[0]
        self.assertEqual((row['positive'], row['accepted']), (1, 0))
        self.library.set_liked('abcde0', False)
        self.assertEqual(self.library.tag_profile(), [])
        self.library.mark_disliked('abcde0')
        row = self.library.tag_profile()[0]
        self.assertEqual((row['negative'], row['accepted']), (1, 0))

    def test_failed_update_and_disabled_personalization_do_not_reward(self):
        self.image('abcde0')
        self.library.observe(['abcde0'])
        self.config.keep = 1
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde1'], fail=True)), 1)
        self.assertEqual(self.library.accepted_feedback(), {})
        self.config.personalized = False
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde1'])), 0)
        self.assertEqual(self.library.accepted_feedback(), {})
        self.assertEqual(self.library.delivery_count(), 0)

    def test_normal_successful_update_records_acceptance(self):
        self.image('abcde0')
        self.library.observe(['abcde0'])
        self.config.keep = 1
        self.assertEqual(update(self.config, self.library, FakeClient(['abcde1'])), 0)
        self.assertEqual(set(self.library.accepted_feedback()), {'abcde0'})
        self.assertEqual(self.library.delivery_count(), 1)


class SourcesClient(FakeClient):
    def __init__(self, all_preferred=False, fail=None):
        self.all_preferred, self.fail = all_preferred, fail
        self.calls = []

    def candidates(self, config):
        prefix = 'pref' if config.query else 'base'
        self.calls.append(config.query)
        yield from FakeClient([f'{prefix}{index:02}' for index in range(50)]).candidates(config)

    def tags(self, wid):
        return tags(1 if self.all_preferred or wid.startswith('pref') else 2)

    def fetch(self, url, sink, limit):
        wid = url.rsplit('/', 1)[-1].split('.')[0]
        if wid == self.fail:
            raise OSError('fixture failure')
        Image.new('RGB', (32, 18), (40 if wid.startswith('pref') else 80, int(wid[-2:]), 100)).save(sink, 'PNG')


class DiscoveryTests(TemporaryLibrary):
    def test_seen_history_and_delivery_are_committed_together_and_idempotent(self):
        self.library.remember('abcde0', 'digest', 'discovery')
        self.library.remember('abcde0', 'digest', 'discovery')
        self.assertTrue(self.library.seen(wid='abcde0'))
        self.assertEqual(self.library.delivery_count(), 1)

    def test_duplicate_content_does_not_consume_exploration_slot(self):
        self.config.keep = 1
        self.library.set_tag_override('sky', 'prefer')
        self.image('oldimg', color=(80, 0, 100))  # Same bytes as base00.
        for wid in ['old000', 'old001', 'old002']:
            self.library.record_delivery(wid, 'preference')
        self.assertEqual(update(self.config, self.library, SourcesClient()), 0)
        with self.library.connect() as db:
            row = db.execute('SELECT id, source FROM deliveries ORDER BY sequence DESC LIMIT 1').fetchone()
        self.assertEqual(row, ('base01', 'exploration'))
        self.assertEqual(self.library.delivery_count(), 4)

    def test_single_download_updates_keep_exploration_across_restarts(self):
        self.config.keep = 1
        self.library.set_tag_override('sky', 'prefer')
        for index in range(12):
            self.assertEqual(update(self.config, self.library, SourcesClient()), 0)
            self.library = Library(self.library.directory, self.library.state)
            self.assertEqual(self.library.delivery_count(), index + 1)
        with self.library.connect() as db:
            rows = db.execute('SELECT id, source FROM deliveries ORDER BY sequence').fetchall()
        for index, (wid, source) in enumerate(rows):
            if (index + 1) % 4 == 0:
                self.assertEqual(source, 'exploration')
                self.assertTrue(wid.startswith('base'))
            else:
                self.assertEqual(source, 'preference')

    def test_failed_exploration_does_not_consume_exploration_slot(self):
        self.library.set_tag_override('sky', 'prefer')
        self.config.keep = 1
        for wid in ['old000', 'old001', 'old002']:
            self.library.record_delivery(wid, 'preference')
        self.assertEqual(update(self.config, self.library, SourcesClient(fail='base00')), 0)
        with self.library.connect() as db:
            row = db.execute('SELECT id, source FROM deliveries ORDER BY sequence DESC LIMIT 1').fetchone()
        self.assertEqual(row, ('base01', 'exploration'))
        self.assertEqual(self.library.delivery_count(), 4)

    def test_exploration_refills_discovery_when_mixed_pool_has_only_biased_items(self):
        self.config.keep = self.config.batch = 12
        self.library.set_tag_override('sky', 'prefer')
        self.assertEqual(update(self.config, self.library, SourcesClient(all_preferred=True)), 0)
        with self.library.connect() as db:
            rows = db.execute('SELECT id, source FROM deliveries WHERE sequence % 4=0').fetchall()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(wid.startswith('base') and source == 'exploration' for wid, source in rows))
