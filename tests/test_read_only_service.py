"""Query-only deployment cannot wake a model or change the existing database."""
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from goldfish.engine import MemoryDB
from goldfish.server import Service


class ReadOnlyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'memory.db'
        db = MemoryDB(self.path)
        mid = db.add_message('fixture', 'user', '合成编号ZX-91属于小周')
        self.sid = db.add_summary('编号ZX-91属于小周', [mid])
        db.close()
        self.service = Service(self.path, read_only=True)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_queries_work_and_pending_raw_sources_do_not_start_jobs(self):
        result = self.service.handle('/search', {'query': 'ZX-91'})
        self.assertEqual(result['records'][0]['id'], self.sid)
        self.assertEqual(self.service.pending, set())
        self.assertTrue(self.service.handle('/health', {})['read_only'])
        self.assertEqual(self.service.db.db.total_changes, 0)

    def test_sqlite_itself_refuses_writes(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.service.db.add_message('fixture', 'user', '不能新增')

    def test_all_background_write_routes_are_refused(self):
        for route in ('/ingest', '/schedule', '/consolidate', '/reindex'):
            with self.subTest(route=route), self.assertRaisesRegex(ValueError, 'read_only'):
                self.service.handle(route, {})

    def test_even_inherited_model_configuration_is_ignored(self):
        with patch.dict(os.environ, {'GOLDFISH_LLM_PROVIDER': 'invalid-provider',
                                    'GOLDFISH_EMBED_PROVIDER': 'invalid-provider'}):
            service = Service(self.path, read_only=True)
        try:
            self.assertFalse(service.chat.configured)
            self.assertFalse(service.embeddings.configured)
        finally: service.close()

    def test_missing_database_is_not_created_or_migrated(self):
        missing = Path(self.temp.name) / 'missing.db'
        with self.assertRaises(sqlite3.OperationalError): MemoryDB(missing, read_only=True)
        self.assertFalse(missing.exists())
