import json
from pathlib import Path
import tempfile
import unittest

from perch.appearance import desktop_font_family


class DesktopFontTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'DankMaterialShell/settings.json'
        self.path.parent.mkdir()

    def test_niri_uses_explicit_desktop_font_instead_of_gtk_default(self):
        self.path.write_text(json.dumps({'fontFamily': 'Maple Mono NF CN'}))
        self.assertEqual(desktop_font_family(self.root, 'niri'), 'Maple Mono NF CN')
        self.assertIsNone(desktop_font_family(self.root, 'GNOME'))

    def test_missing_empty_or_partially_written_desktop_file_falls_back_to_gtk(self):
        self.assertIsNone(desktop_font_family(self.root, 'niri'))
        for contents in ('{', '[]', '{}', '{"fontFamily": " "}', '{"fontFamily": 12}'):
            self.path.write_text(contents)
            self.assertIsNone(desktop_font_family(self.root, 'niri'))
