"""Архив сырых ответов API: запись, чтение, live-снимки."""
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings

from parsers.sportmonks import archive


class ArchiveTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.override = override_settings(API_ARCHIVE_DIR=Path(self.tmp.name))
        self.override.enable()
        self.addCleanup(self.override.disable)

    def test_roundtrip(self):
        path = archive.save({"id": 1, "name": "Кайрат"}, "fixtures", "2026", "1")
        self.assertEqual(path, archive.path_for("fixtures", "2026", "1"))
        self.assertEqual(archive.load(path), {"id": 1, "name": "Кайрат"})

    def test_live_snapshots(self):
        archive.record_live([{"id": 5}])
        archive.record_live([])  # пустой опрос не пишется
        archive.record_fixture({"id": 5, "events": []})
        files = sorted(p.name for p in Path(self.tmp.name).rglob("*.json.gz"))
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f.startswith("fixture_5_") for f in files))
