from dataclasses import replace
from test_core import TemporaryLibrary, FakeClient
from perch import colors
from perch.downloader import eligible, Client
from perch.recommendation import candidates, TagFetcher


class ColorTests(TemporaryLibrary):
    def seed(self, count=20):
        for i in range(count):
            wid = f'tone{i:02d}'
            with self.library.connect() as db:
                db.execute('INSERT INTO likes(id) VALUES (?)', (wid,))
            colors.save_palette(self.library, wid, ['#0066cc', '#333399', '#ffffff'])

    def test_families_deduplicate_similar_accents_and_ignore_invalid_values(self):
        self.assertEqual(colors.families(['#0066cc', '#0099cc', '#cc3333']), {'blue': 1., 'red': .3})
        self.assertEqual(colors.normalize(['#FFFFFF', '#ffffff', 'invalid', None]), ['ffffff'])
        for key, (_, swatch) in colors.TONES.items():
            self.assertEqual(colors.family(swatch), key)

    def test_calibration_manual_override_and_sample_dedup(self):
        self.seed(19)
        samples, profile = colors.learning_model(self.library)
        self.assertEqual(samples, 19)
        self.assertTrue(all(row['weight'] == 0 for row in profile))
        colors.set_override(self.library, 'red', 'prefer')
        self.assertEqual(next(row for row in colors.learning_model(self.library)[1] if row['key'] == 'red')['weight'], 1.)
        with self.library.connect() as db:
            db.execute("INSERT INTO remote_likes VALUES ('tone00')")
            db.execute("INSERT INTO likes(id) VALUES ('tone19')")
        colors.save_palette(self.library, 'tone19', ['0066cc'])
        samples, profile = colors.learning_model(self.library)
        self.assertEqual(samples, 20)
        blue = next(row for row in profile if row['key'] == 'blue')
        self.assertEqual(blue['positive'], 20)
        self.assertGreater(blue['weight'], 0)

    def test_ignore_removes_color_from_score_and_sample_count(self):
        self.seed()
        for key in ['blue', 'white']:
            colors.set_override(self.library, key, 'ignore')
        self.assertEqual(colors.learning_model(self.library)[0], 0)
        profile = colors.learning_model(self.library)[1]
        self.assertEqual(colors.score(['0066cc', 'ffffff'], profile), 0)
        colors.set_override(self.library, 'blue', None)
        self.assertEqual(colors.learning_model(self.library)[0], 20)

    def test_negative_feedback_and_minimum_family_evidence(self):
        self.seed()
        with self.library.connect() as db:
            db.execute("INSERT INTO dislikes(id) VALUES ('bad000')")
        colors.save_palette(self.library, 'bad000', ['cc3333'])
        profile = colors.learning_model(self.library)[1]
        self.assertEqual(next(row for row in profile if row['key'] == 'red')['weight'], 0)
        for wid in ['bad001', 'bad002']:
            with self.library.connect() as db:
                db.execute('INSERT INTO dislikes(id) VALUES (?)', (wid,))
            colors.save_palette(self.library, wid, ['cc3333'])
        self.assertEqual(next(row for row in colors.learning_model(self.library)[1] if row['key'] == 'red')['weight'], 0)
        with self.library.connect() as db:
            db.execute("INSERT INTO dislikes(id) VALUES ('bad003')")
        colors.save_palette(self.library, 'bad003', ['cc3333'])
        self.assertLess(next(row for row in colors.learning_model(self.library)[1] if row['key'] == 'red')['weight'], 0)

    def test_color_preference_ranks_before_calibration_preserves_filters_and_queries(self):
        colors.set_override(self.library, 'blue', 'prefer')
        client = FakeClient(['abcde0', 'abcde1'])
        original = client.candidates
        searches = []
        def search(config):
            searches.append((config.query, config.color, config.min_width))
            for item in original(config):
                yield dict(item, colors=['ff9900'] if item['id'] == 'abcde0' else ['0066cc'])
        client.candidates = search
        ordered = list(candidates(self.config, self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual([item['id'] for item in ordered], ['abcde1', 'abcde0'])
        self.assertEqual(searches, [('', '', 32), ('', '0066cc', 32)])
        searches.clear()
        list(candidates(replace(self.config, query='sky'), self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual(searches, [('sky', '', 32)])
        ordered = list(candidates(replace(self.config, personalized=False), self.library, client, TagFetcher(self.library, client), eligible))
        self.assertEqual([item['id'] for item in ordered], ['abcde0', 'abcde1'])
