import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from goldfish.engine import MemoryDB
from goldfish.server import Service
from goldfish import queries, remember


def payload(text='请记住：B2验收红鱼编号XY-27。', quote='B2验收红鱼编号XY-27。', seq=3):
    source = dict(namespace='test-profile', session_id='test-session', message_id='msg-' + str(seq),
                  event_seq=seq, event_time='2026-11-05T01:00:00.000Z', text=text, role='user', source_kind='user')
    return dict(version=1, issued_at=time.time(), quote=quote, request_quote='请记住',
                source=source, authorization=source.copy(), previous_user_message_id=None)


class ExplicitRememberTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'memory.sqlite3'
        self.db = MemoryDB(self.path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def counts(self):
        return [self.db.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                for table in ('raw_messages', 'summaries', 'evidence', 'explicit_memory_claims', 'explicit_memory_receipts')]

    def test_source_receipt_dedup_and_search(self):
        data = payload()
        first = remember.save(self.db, data)
        second = remember.save(self.db, data)
        self.assertTrue(first['saved'])
        self.assertEqual(first['memory_id'], second['memory_id'])
        self.assertEqual(first['receipt_id'], second['receipt_id'])
        self.assertTrue(second['duplicate'] and second['replayed_receipt'])
        self.assertEqual(self.counts(), [1, 1, 1, 1, 1])
        rows = queries.search(self.db, {'query': 'XY-27'})['records']
        self.assertEqual(rows[0]['text'], data['quote'])
        self.assertEqual(rows[0]['kind'], 'explicit_user_statement')
        self.assertEqual(rows[0]['provenance'], 'user_requested_quote')
        self.assertIn('not_independently_verified', rows[0]['verification'])
        original = self.db.evidence_for(first['memory_id'])[0]
        self.assertEqual(original['text'], data['source']['text'])
        self.assertEqual(original['event_time'], data['source']['event_time'])
        self.assertEqual(len(self.db.get_unprocessed_messages('test-profile:test-session')), 1,
                         'saving one quote must not consume the rest of its source message')

    def test_previous_message_new_request_same_claim_new_receipt(self):
        data = payload()
        first = remember.save(self.db, data)
        data['authorization'] = payload('请记住我上一条消息。', seq=9)['source']
        data['request_quote'] = '请记住我上一条消息'
        data['previous_user_message_id'] = data['source']['message_id']
        second = remember.save(self.db, data)
        self.assertEqual(first['memory_id'], second['memory_id'])
        self.assertNotEqual(first['receipt_id'], second['receipt_id'])
        self.assertEqual(self.counts(), [2, 1, 2, 1, 2])

    def test_identical_text_different_events_not_merged(self):
        first = remember.save(self.db, payload())
        second = remember.save(self.db, payload(seq=10))
        self.assertNotEqual(first['memory_id'], second['memory_id'])
        self.assertEqual(self.counts()[1], 2)

    def test_receipt_survives_reopen_without_advancing_source_cursor(self):
        first = remember.save(self.db, payload())
        reopened = MemoryDB(self.path)
        try:
            again = remember.save(reopened, payload())
            self.assertEqual(first['receipt_id'], again['receipt_id'])
            self.assertEqual(first['saved_at'], again['saved_at'])
            self.assertTrue(again['replayed_receipt'])
            self.assertEqual(reopened.db.execute('SELECT COUNT(*) FROM consolidation_jobs').fetchone()[0], 0)
        finally: reopened.close()

    def test_atomic_rollback(self):
        real = self.db.add_summary
        def interrupted(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError('fixture interruption')
        with patch.object(self.db, 'add_summary', interrupted):
            with self.assertRaises(RuntimeError): remember.save(self.db, payload())
        self.assertEqual(self.counts(), [0, 0, 0, 0, 0])

    def test_reject_guesses_nonhuman_cross_session_mismatched_quote_and_reused_source(self):
        for mutation in (
            lambda d: d.update(quote='助手推测的未经确认的心理解释'),
            lambda d: d['source'].update(role='assistant'),
            lambda d: d['source'].update(source_kind='plugin:example'),
            lambda d: d['source'].update(session_id='another-session'),
            lambda d: d.update(previous_user_message_id='forged', authorization=payload('请记住我上一条消息。', seq=9)['source']),
        ):
            data = payload(); mutation(data)
            with self.assertRaises(ValueError): remember.save(self.db, data)
            self.assertEqual(self.counts(), [0, 0, 0, 0, 0])
        remember.save(self.db, payload())
        changed = payload(text='请记住：B2验收红鱼编号XY-27。另加一段篡改。')
        with self.assertRaises(ValueError): remember.save(self.db, changed)
        self.assertEqual(self.counts(), [1, 1, 1, 1, 1])

    def test_conservative_explicit_intent(self):
        for text, excerpt in (
            ('请记住：验收红鱼。', '请记住'),
            ('帮我记住这句“验收红鱼”。', '帮我记住'),
            ('Please remember: red fish.', 'Please remember'),
            ('请记住：验收红鱼。无需使用其他工具。', '请记住'),
        ):
            self.assertTrue(remember.explicit_request(text, excerpt), text)
        for text, excerpt in (
            ('不要记住：验收红鱼。', '记住'),
            ('请记住：验收红鱼。但不要实际保存。', '请记住'),
            ('文档写着“请记住：验收红鱼”。', '请记住'),
            ('```\n请记住：验收红鱼。\n```', '请记住'),
            ('> 请记住：验收红鱼。', '请记住'),
            ('例如：请记住：验收红鱼。', '请记住'),
            ('你还记住了什么？', '记住'),
            ('Do you remember red fish?', 'remember'),
        ):
            self.assertFalse(remember.explicit_request(text, excerpt), text)

    def test_service_signature_models_disabled_and_reader_cannot_write(self):
        key = Path(self.temp.name) / 'write.key'
        service = Service(self.path, read_only=True, allow_remember=True, remember_key_file=key)
        try:
            self.assertFalse(service.chat.configured or service.embeddings.configured)
            self.assertEqual(service.pending, set())
            health = service.handle('/health', {})
            self.assertTrue(health['query_only'] and health['explicit_remember'])
            self.assertFalse(health['read_only'])
            with self.assertRaises(sqlite3.OperationalError): service.db.db.execute("DELETE FROM summaries")
            for route in ('/ingest', '/schedule', '/consolidate', '/remember', '/reindex'):
                with self.assertRaises(ValueError): service.handle(route, {})
            raw = json.dumps(payload(), ensure_ascii=False).encode()
            sign = lambda body: hmac.new(key.read_bytes(), body, hashlib.sha256).hexdigest()
            for signature in ('', 'f' * 64):
                with self.assertRaises(PermissionError): service.remember_signed(raw, signature)
            self.assertTrue(service.remember_signed(raw, sign(raw))['saved'])
            expired = payload(); expired['issued_at'] -= 600
            raw = json.dumps(expired).encode()
            with self.assertRaises(PermissionError): service.remember_signed(raw, sign(raw))
        finally: service.close()

    def test_read_only_schema3_compat_and_additive_schema4_migration(self):
        self.db.db.execute('DROP TABLE explicit_memory_receipts')
        self.db.db.execute('DROP TABLE explicit_memory_claims')
        self.db.db.execute('PRAGMA user_version=3')
        reader = MemoryDB(self.path, read_only=True)
        self.assertEqual(queries.search(reader, {'query': 'empty'})['records'], [])
        reader.close()
        migrated = MemoryDB(self.path)
        self.assertEqual(migrated.db.execute('PRAGMA user_version').fetchone()[0], 4)
        self.assertEqual(migrated.db.execute('PRAGMA foreign_key_check').fetchall(), [])
        migrated.close()
