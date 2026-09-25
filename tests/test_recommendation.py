from dataclasses import replace
import json
from unittest.mock import patch

from test_core import TemporaryLibrary, FakeClient
from perch.config import Config, load
from perch.downloader import Client, eligible, update, replace_wallpaper
from perch.library import Library
from perch.recommendation import TagFetcher, candidates, score_tags, order_pool, sync_feedback


def tags(*ids):
    names = {1: 'sky', 2: 'city', 3: 'forest'}
    return [dict(id=tid, name=names.get(tid, str(tid))) for tid in ids]


class TaggedClient(FakeClient):
    def __init__(self, ids=(), metadata=None):
        super().__init__(ids, 'unique')
        self.metadata = metadata or {}
        self.tag_calls = []
        self.queries = []

    def tags(self, wid):
        self.tag_calls.append(wid)
        return self.metadata.get(wid, [])

    def candidates(self, config):
        self.queries.append(config.query)
        yield from super().candidates(config)


class FeedbackTests(TemporaryLibrary):
    def test_liked_image_can_be_pruned_and_feedback_survives(self):
        old = self.image('abcde0', modified=1)
        self.image('abcde1', modified=2)
        self.library.set_liked('abcde0', True)
        self.library.save_tags('abcde0', tags(1))
        self.library.prune(1)
        self.assertFalse(old.exists())
        self.assertEqual(self.library.feedback(), {'abcde0': 1})
        self.assertGreater(self.library.tag_profile()[0]['weight'], 0)

    def test_like_and_favorite_are_independent(self):
        self.image('abcde0')
        self.library.set_liked('abcde0', True)
        self.library.set_favorite('abcde0', True)
        self.library.save_tags('abcde0', tags(1))
        self.assertEqual(self.library.tag_profile()[0]['positive'], 1)
        self.library.set_liked('abcde0', False)
        self.assertTrue(self.library.items()[0].favorite)
        self.library.prune(0)
        self.assertEqual(len(self.library.items()), 1)
        self.library.set_liked('abcde0', True)
        self.library.set_favorite('abcde0', False)
        self.assertTrue(self.library.items()[0].liked)
        self.library.prune(0)
        self.assertEqual(len(self.library.items()), 0)

    def test_favorites_can_be_excluded_from_personalization(self):
        self.image('abcde0')
        self.library.set_favorite('abcde0', True)
        self.library.save_tags('abcde0', tags(1))
        self.assertEqual(self.library.feedback(False), {})
        self.assertEqual(self.library.tag_profile(False), [])
        self.assertTrue(self.library.items()[0].favorite)

    def test_dislike_replaces_positive_feedback_and_like_can_undo_it(self):
        self.image('abcde0')
        self.library.save_tags('abcde0', tags(1))
        self.library.set_liked('abcde0', True)
        self.library.mark_disliked('abcde0')
        self.assertEqual(self.library.feedback(), {'abcde0': -1})
        self.assertFalse(self.library.items()[0].liked)
        self.assertLess(self.library.tag_profile()[0]['weight'], 0)
        self.library.set_liked('abcde0', True)
        self.assertFalse(self.library.items()[0].disliked)
        self.assertGreater(self.library.tag_profile()[0]['weight'], 0)
        self.library.set_liked('abcde0', False)
        self.assertEqual(self.library.tag_profile(), [])

    def test_old_favorites_stay_protected_without_becoming_likes(self):
        self.image('abcde0')
        self.library.set_favorite('abcde0', True)
        reopened = Library(self.library.directory, self.library.state)
        self.assertTrue(reopened.items()[0].favorite)
        self.assertFalse(reopened.items()[0].liked)
        reopened.prune(0)
        self.assertEqual(len(reopened.items()), 1)

    def test_old_config_gets_new_defaults(self):
        path = self.root / 'old.json'
        path.write_text('{"keep": 20, "batch": 3}')
        config = load(path)
        self.assertTrue(config.personalized)
        self.assertTrue(config.favorites_influence)

    def test_duplicate_tags_do_not_double_count(self):
        self.image('abcde0')
        self.library.set_liked('abcde0', True)
        self.library.save_tags('abcde0', tags(1, 1, 2) + [{'id': True, 'name': 'bad'}])
        self.assertEqual(len(self.library.tags_for('abcde0')), 2)
        self.assertEqual(self.library.tag_profile()[0]['positive'], 1)


class TagCacheTests(TemporaryLibrary):
    def test_cache_avoids_repeated_requests_even_for_empty_tags(self):
        client = TaggedClient(metadata={'abcde0': tags(1)})
        fetcher = TagFetcher(self.library, client)
        for _ in range(2):
            self.assertEqual(fetcher.get('abcde0'), tags(1))
            self.assertEqual(fetcher.get('abcde1'), [])
        self.assertEqual(client.tag_calls, ['abcde0', 'abcde1'])

    def test_fetches_feedback_tags_after_image_has_been_deleted(self):
        self.image('abcde0')
        self.library.set_liked('abcde0', True)
        self.library.prune(0)
        client = TaggedClient(metadata={'abcde0': tags(1)})
        sync_feedback(self.config, self.library, TagFetcher(self.library, client))
        self.assertEqual(self.library.tags_for('abcde0'), tags(1))
        self.assertGreater(self.library.tag_profile()[0]['weight'], 0)

    def test_failure_keeps_feedback_and_stops_metadata_requests_for_run(self):
        self.image('abcde0')
        self.library.set_liked('abcde0', True)
        client = TaggedClient()
        with patch.object(client, 'tags', side_effect=OSError('offline')) as request:
            fetcher = TagFetcher(self.library, client)
            self.assertIsNone(fetcher.get('abcde0'))
            self.assertIsNone(fetcher.get('abcde1'))
            self.assertEqual(request.call_count, 1)
        self.assertFalse(self.library.tags_due('abcde0'))
        self.assertEqual(self.library.feedback(), {'abcde0': 1})
        self.library.save_tags('abcde2', tags(2))
        self.assertEqual(fetcher.get('abcde2'), tags(2))

    def test_client_validates_detail_identity(self):
        client = Client()
        def fetch(url, sink, limit):
            self.assertEqual(url, 'https://wallhaven.cc/api/v1/w/abcde0')
            sink.write(json.dumps({'data': {'id': 'abcde1', 'tags': tags(1)}}).encode())
        client.fetch = fetch
        with self.assertRaises(ValueError):
            client.tags('abcde0')


class RankingTests(TemporaryLibrary):
    def seed_feedback(self):
        self.image('likes0')
        self.library.set_liked('likes0', True)
        self.library.save_tags('likes0', tags(1))
        self.image('hates0')
        self.library.mark_disliked('hates0')
        self.library.save_tags('hates0', tags(2))
        return self.library.tag_profile()

    def test_preferred_then_unseen_then_negative_tags(self):
        profile = self.seed_feedback()
        pool = [({'id': 'city00'}, tags(2)), ({'id': 'none00'}, tags(3)), ({'id': 'sky000'}, tags(1))]
        self.assertEqual([item['id'] for item in order_pool(pool, profile)], ['sky000', 'none00', 'city00'])
        self.assertEqual(score_tags(tags(1, 1), profile), score_tags(tags(1), profile))

    def test_every_fourth_result_keeps_room_for_exploration(self):
        profile = self.seed_feedback()
        pool = [({'id': 'unknown'}, [])] + [({'id': f'sky{i}'}, tags(1)) for i in range(6)]
        ordered = order_pool(pool, profile)
        self.assertEqual(ordered[3]['id'], 'unknown')
        self.assertEqual(len(ordered), len(pool))

    def test_tag_searches_mix_with_base_and_deduplicate(self):
        self.seed_feedback()
        client = TaggedClient(['abcde0', 'abcde1', 'abcde2'],
                              {'abcde0': tags(2), 'abcde1': tags(1), 'abcde2': tags(3)})
        ordered = list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual([item['id'] for item in ordered], ['abcde1', 'abcde2', 'abcde0'])
        self.assertEqual(client.queries, ['', 'id:1'])

    def test_explicit_query_is_never_overridden(self):
        self.seed_feedback()
        self.config.query = '+landscape -city'
        client = TaggedClient(['abcde0'], {'abcde0': tags(1)})
        list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual(client.queries, ['+landscape -city'])

    def test_disabling_personalization_preserves_source_order(self):
        self.seed_feedback()
        self.config.personalized = False
        client = TaggedClient(['abcde0', 'abcde1'], {'abcde0': tags(2), 'abcde1': tags(1)})
        ordered = list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual([item['id'] for item in ordered], ['abcde0', 'abcde1'])
        self.assertEqual(client.tag_calls, [])
        self.assertEqual(client.queries, [''])

    def test_auxiliary_search_failure_falls_back_to_normal_discovery(self):
        self.seed_feedback()
        client = TaggedClient(['abcde0'], {'abcde0': tags(1)})
        original = client.candidates
        def search(config):
            if config.query:
                raise OSError('tag lookup unavailable')
            yield from original(config)
        client.candidates = search
        result = list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual([item['id'] for item in result], ['abcde0'])

    def test_personalization_never_relaxes_resolution_or_sfw_filters(self):
        self.seed_feedback()
        client = TaggedClient()
        def search(config):
            for item in FakeClient(['abcde0']).candidates(config):
                yield dict(item, purity='nsfw')
                yield dict(item, dimension_x=1)
                yield dict(item, path='https://example.com/test.png')
        client.candidates = search
        self.assertEqual(list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible)), [])
        self.assertEqual(client.tag_calls, [])

    def test_actual_download_uses_personalized_order(self):
        self.seed_feedback()
        self.config.keep = self.config.batch = 1
        client = TaggedClient(['abcde0', 'abcde1'], {'abcde0': tags(2), 'abcde1': tags(1)})
        self.assertEqual(update(self.config, self.library, client), 0)
        self.assertTrue(self.library.seen(wid='abcde1'))
        self.assertFalse(self.library.seen(wid='abcde0'))
        self.assertEqual(self.library.tags_for('abcde1'), tags(1))

    def test_disliked_tags_are_saved_before_replacement_removes_file(self):
        original = self.image('abcde0')
        client = TaggedClient(['abcde1'], {'abcde0': tags(2), 'abcde1': tags(1)})
        replace_wallpaper(self.config, self.library, 'abcde0', client)
        self.assertFalse(original.exists())
        self.assertEqual(self.library.tags_for('abcde0'), tags(2))
        self.assertLess(self.library.tag_profile()[0]['weight'], 0)
