import io
import json
import os
from dataclasses import replace
from unittest.mock import patch
import urllib.error
import urllib.request

from test_core import TemporaryLibrary, FakeClient
from perch.accounts import Account, account_path, load_account, save_account, list_collections, sync_collections
from perch.downloader import Client, PrivateAPIRedirect
from perch.recommendation import TagFetcher
from perch import colors


class CollectionClient(FakeClient):
    def __init__(self, ids=('abcde0',), fail=False):
        super().__init__(ids, 'unique', fail)
        self.downloads = []

    def collections(self, username):
        return [dict(id=1, label='风景'), dict(id=2, label='夜色')]

    def collection_items(self, username, cid):
        yield from self.candidates(type('Config', (), {})())

    def detail(self, wid):
        return dict(id=wid, tags=[dict(id=1, name='sky')], colors=['#0066cc'])

    def fetch(self, url, sink, limit):
        self.downloads.append(url)
        super().fetch(url, sink, limit)


class CollectionTests(TemporaryLibrary):
    def account(self, mode='favorite'):
        return Account('example', collections={'1': dict(label='风景', mode=mode)})

    def sync(self, account=None, client=None):
        return sync_collections(self.config, self.library, account or self.account(), client or CollectionClient())

    def test_secret_separate_masked_repr_and_owner_only_file(self):
        account = self.account()
        account.api_key = 'exampleSECRET'
        path = account_path(self.root / 'config.json')
        save_account(account, path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(load_account(path).api_key, account.api_key)
        self.assertNotIn(account.api_key, repr(account))
        self.assertEqual(list_collections(account, CollectionClient())['1']['mode'], 'favorite')
        path.write_text('{"api_key": "exampleSECRET", "bad": 1}')
        with self.assertRaisesRegex(ValueError, '账户设置无法读取') as error:
            load_account(path)
        self.assertNotIn('exampleSECRET', str(error.exception))

    def test_favorite_import_is_incremental_protected_and_counted_once(self):
        client = CollectionClient()
        result = self.sync(client=client)
        self.assertEqual((result['imported'], result['downloaded']), (1, 1))
        self.assertTrue(self.library.items()[0].favorite)
        self.assertTrue(self.library.items()[0].liked)
        self.assertEqual(self.library.feedback(), {'abcde0': 1})
        self.assertEqual(self.library.tag_profile()[0]['positive'], 1)
        self.library.prune(0)
        result = self.sync(client=client)
        self.assertEqual(result['unchanged'], 1)
        self.assertEqual(len(client.downloads), 1)

    def test_preference_only_never_downloads_and_can_upgrade(self):
        result = self.sync(self.account('learn'))
        self.assertEqual(result['downloaded'], 0)
        self.assertFalse(self.library.items())
        self.assertEqual(self.library.feedback(), {'abcde0': 1})
        self.assertEqual(colors.palette(self.library, 'abcde0'), ['0066cc'])
        self.sync()
        self.assertTrue(self.library.items()[0].favorite)

    def test_cleaned_up_like_is_not_redownloaded_or_recreated_as_favorite(self):
        self.sync(self.account('like'))
        self.library.prune(0)
        self.sync(self.account('like'))
        self.assertFalse(self.library.items())
        self.assertEqual(self.library.feedback(), {'abcde0': 1})

    def test_local_unlike_wins_even_when_new_collection_contains_same_image(self):
        self.sync()
        self.library.set_liked('abcde0', False)
        other = Account('example', collections={'2': dict(label='other', mode='favorite')})
        self.sync(other)
        self.assertFalse(self.library.items()[0].favorite)
        self.assertFalse(self.library.items()[0].liked)
        self.assertEqual(self.library.feedback(), {})
        self.library.prune(0)
        self.sync(other)
        self.assertFalse(self.library.items())

    def test_unfavorite_survives_new_collection_but_like_remains(self):
        self.sync()
        self.library.set_favorite('abcde0', False)
        self.sync(Account('example', collections={'2': dict(label='other', mode='favorite')}))
        self.assertFalse(self.library.items()[0].favorite)
        self.assertTrue(self.library.items()[0].liked)

    def test_remote_removal_and_mode_downgrade_do_not_delete_or_unprotect(self):
        self.sync()
        self.sync(client=CollectionClient([]))
        self.sync(self.account('learn'))
        self.assertTrue(self.library.items()[0].favorite)

    def test_disliked_image_never_downloaded_and_filters_preserved(self):
        self.image('abcde0')
        self.library.mark_disliked('abcde0')
        client = CollectionClient()
        self.sync(client=client)
        self.assertFalse(client.downloads)
        self.config.min_width = 3840
        self.assertEqual(self.sync(client=CollectionClient(['abcde1']))['skipped'], 1)
        self.assertEqual(self.library.feedback()['abcde0'], -1)

    def test_download_failure_preserves_library_and_can_retry(self):
        self.image('local0')
        with self.assertRaises(OSError):
            self.sync(client=CollectionClient(fail=True))
        self.assertEqual(len(self.library.items()), 1)
        self.assertFalse(list(self.library.directory.glob('*.part')))
        with self.library.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM collection_imports').fetchone()[0], 0)
        self.assertEqual(self.sync()['downloaded'], 1)

    def test_local_unlike_during_download_prevents_install_and_import(self):
        self.image('abcde0')
        self.library.set_liked('abcde0', True)
        self.library.prune(0)
        client = CollectionClient()
        fetch = client.fetch
        def concurrent(url, sink, limit):
            fetch(url, sink, limit)
            self.library.set_liked('abcde0', False)
        client.fetch = concurrent
        self.assertEqual(self.sync(client=client)['downloaded'], 0)
        self.assertFalse(self.library.items())
        self.assertEqual(self.library.feedback(), {})

    def test_pagination_and_public_private_routes(self):
        client = Client()
        calls = []
        def api(path, params=None):
            calls.append((path, params))
            page = params['page'] if params else 1
            return {'data': [{'id': f'abcde{page}'}], 'meta': {'last_page': 2}}
        client.api = api
        client.collections('example')
        self.assertEqual(calls[-1][0], 'collections/example')
        client.api_key = 'secret'
        client.collections('example')
        self.assertEqual(calls[-1][0], 'collections')
        rows = list(client.collection_items('example', '12'))
        self.assertEqual([row['id'] for row in rows], ['abcde1', 'abcde2'])
        self.assertEqual(calls[-1], ('collections/example/12', {'page': 2, 'purity': '100'}))

    def test_auth_header_only_to_official_api_and_no_redirects(self):
        client = Client('secret')
        requests = []
        def open_url(request, timeout):
            requests.append(request)
            response = io.BytesIO(b'{}')
            response.headers = {}
            return response
        with patch('urllib.request.urlopen', side_effect=open_url), patch('urllib.request.build_opener') as opener, patch('time.sleep'):
            opener.return_value.open.side_effect = open_url
            for url in ['https://wallhaven.cc/api/v1/collections', 'https://w.wallhaven.cc/full/aa/test.png',
                        'https://other.example/api/v1/collections', 'https://wallhaven.cc/other']:
                client.fetch(url, io.BytesIO(), 100)
        self.assertEqual([r.get_header('X-api-key') for r in requests], ['secret', None, None, None])
        self.assertTrue(all('secret' not in r.full_url for r in requests))
        with self.assertRaises(urllib.error.URLError):
            PrivateAPIRedirect().redirect_request(requests[0], None, 302, '', {}, 'https://other.example')

    def test_metadata_backfills_color_when_tags_already_cached(self):
        self.library.save_tags('abcde0', [dict(id=1, name='sky')])
        fetcher = TagFetcher(self.library, CollectionClient())
        fetcher.get('abcde0')
        self.assertEqual(colors.palette(self.library, 'abcde0'), ['0066cc'])
        self.assertFalse(fetcher.due('abcde0'))
