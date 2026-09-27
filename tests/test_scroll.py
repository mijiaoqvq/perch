"""Retained log text detection, including repeated entries and Unicode."""
import random
import unittest

from perch.widgets import text_overlap


class LogOverlapTests(unittest.TestCase):
    def test_log_changes(self):
        for previous, current, expected in (
            ('', '', 0), ('', 'first\n', 0), ('old', '', 0),
            ('old\n', 'old\nnew\n', 4), ('old\n', 'old\n', 4),
            ('one\ntwo\n', 'two\nthree\n', 4), ('before', 'rotated', 0),
            ('旧记录\n下载完成\n', '下载完成\n新增记录\n', 5),
            ('repeat\n' * 10000, 'repeat\n' * 10000 + 'new\n', 70000),
            ('repeat\n' * 10000, 'repeat\n' * 9000 + 'new\n', 63000),
        ):
            with self.subTest(previous=previous[:30], current=current[:30]):
                self.assertEqual(text_overlap(previous, current), expected)

    def test_repeated_fragments(self):
        randomizer = random.Random(7)
        for _ in range(300):
            previous = ''.join(randomizer.choices('abc\n图', k=randomizer.randrange(30)))
            current = ''.join(randomizer.choices('abc\n图', k=randomizer.randrange(30)))
            expected = max(size for size in range(min(len(previous), len(current)) + 1)
                           if previous.endswith(current[:size]))
            self.assertEqual(text_overlap(previous, current), expected)
