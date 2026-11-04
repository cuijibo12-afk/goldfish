"""Disk, concurrency and effective-context regressions; no model network calls."""
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from goldfish.consolidate import consolidate_session
from goldfish.engine import MemoryDB


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'memory.db'
        self.db = MemoryDB(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def result(self, mid, **changes):
        result = {'summaries': [{'ref': 'n1', 'text': '用户喜欢咖啡',
                                'evidence_ids': [mid], 'theme': '饮食'}],
                  'links': [], 'profiles': []}
        result.update(changes)
        return result

    def provider(self, result):
        class Provider:
            def extract(self, messages, related):
                return result
        return Provider()

    def test_late_database_failure_rolls_back_outputs_and_cursor(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡')
        # Fail after a summary and theme have already been written by helpers.
        self.db.db.execute("""CREATE TRIGGER reject_profile BEFORE INSERT ON profile
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END""")
        result = self.result(mid, profiles=[{'kind': 'user_profile',
                                           'text': '用户喜欢咖啡', 'evidence_ids': [mid]}])
        with self.assertRaises(sqlite3.IntegrityError):
            consolidate_session(self.db, 's', self.provider(result))
        for table in ('summaries', 'themes', 'evidence', 'theme_members', 'consolidation_jobs'):
            self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(len(self.db.get_unprocessed_messages('s')), 1)
        self.db.db.execute('DROP TRIGGER reject_profile')
        self.assertEqual(consolidate_session(self.db, 's', self.provider(result))['summaries'], 1)

    def test_invalid_late_model_field_is_quarantined_without_losing_valid_summary(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡')
        result = self.result(mid)
        result['summaries'].append({'ref': 'n2', 'text': '另一概括',
                                    'evidence_ids': [mid], 'start_time': {'bad': 'value'}})
        outcome=consolidate_session(self.db, 's', self.provider(result))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 1)
        self.assertEqual(len(outcome['rejected_items']),1)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM consolidation_rejections').fetchone()[0],1)
        self.assertEqual(len(self.db.get_unprocessed_messages('s')), 0)

    def test_failed_commit_rolls_back_and_connection_can_retry(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡')
        connection = self.db.db

        class FailedCommit:
            fail = True

            def __getattr__(self, name):
                return getattr(connection, name)

            def commit(self):
                if self.fail:
                    self.fail = False
                    raise sqlite3.OperationalError('synthetic full disk during commit')
                return connection.commit()

        self.db.db = FailedCommit()
        with self.assertRaises(sqlite3.OperationalError):
            consolidate_session(self.db, 's', self.provider(self.result(mid)))
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 0)
        self.assertEqual(len(self.db.get_unprocessed_messages('s')), 1)
        self.assertEqual(consolidate_session(self.db, 's', self.provider(self.result(mid)))['processed'], 1)

    def test_same_session_concurrent_consolidation_extracts_once(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡')
        entered, release = threading.Event(), threading.Event()
        result = self.result(mid)
        calls = []
        class SlowProvider:
            def extract(self, messages, related):
                calls.append(messages)
                entered.set()
                if not release.wait(3): raise TimeoutError('test release failed')
                return result
        with ThreadPoolExecutor(max_workers=3) as pool:
            first = pool.submit(consolidate_session, self.db, 's', SlowProvider())
            self.assertTrue(entered.wait(1))
            second = pool.submit(consolidate_session, self.db, 's', SlowProvider())
            # Slow inference cannot hold the SQLite lock and block raw capture.
            capture = pool.submit(self.db.add_message, 'other', 'user', '独立原话')
            try:
                self.assertIsNotNone(capture.result(timeout=1))
            finally:
                release.set()
            self.assertEqual(first.result(timeout=2)['processed'], 1)
            self.assertEqual(second.result(timeout=2)['processed'], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 1)

    def test_event_replay_is_idempotent_and_conflict_is_rejected(self):
        text = '原话细节' * 8000
        mid = self.db.add_message('s', 'user', text, message_id='event-1')
        self.assertEqual(self.db.get_message(mid)['text'], text)
        self.assertEqual(self.db.add_message('s', 'user', text, message_id='event-1'), mid)
        with self.assertRaises(ValueError):
            self.db.add_message('s', 'user', '不同的原话', message_id='event-1')
        self.assertEqual(self.db.get_message(mid)['text'], text)
        self.assertEqual(len(self.db.get_unprocessed_messages('s')), 1)

    def test_two_database_instances_cannot_commit_same_batch_twice(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡')
        other = MemoryDB(self.path)
        entered, release = threading.Event(), threading.Event()
        result = self.result(mid)
        class SlowProvider:
            def extract(self, messages, related):
                entered.set()
                if not release.wait(3): raise TimeoutError('test release failed')
                return result
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                slow = pool.submit(consolidate_session, self.db, 's', SlowProvider())
                self.assertTrue(entered.wait(1))
                try:
                    self.assertEqual(consolidate_session(other, 's', self.provider(result))['processed'], 1)
                finally:
                    release.set()
                self.assertEqual(slow.result(timeout=2)['processed'], 0)
            self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 1)
        finally:
            other.close()

    def test_human_discussion_of_memory_markup_is_retained(self):
        mid = self.db.add_message('s', 'user', '请解释 <goldfish-memory> 的意义', source='human')
        self.assertIsNotNone(mid)
        self.assertIsNone(self.db.add_message('s', 'user', '内部注入', source='plugin:goldfish-memory'))

    def test_compaction_retaining_only_id_allows_detail_recall(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡')
        sid = self.db.add_summary('用户喜欢咖啡', [mid])
        context = f'压缩仅保留曾使用的记忆编号 gf:{sid}:v1，具体事实已丢失。'
        result = self.db.recall('咖啡', context=context, max_chars=1000)
        self.assertEqual([row['id'] for row in result['results']], [sid])
        self.assertEqual(self.db.recall('咖啡', context='摘要：用户喜欢咖啡')['injection'], '')

    def test_profile_only_injection_has_wrapper_dedup_and_recalls_after_loss(self):
        mid = self.db.add_message('s', 'user', '我希望你交流自然一些')
        self.db.add_profile('interaction_expectation', '用户希望自然交流', [mid])
        first = self.db.recall('如何聊天', max_chars=500)['injection']
        self.assertTrue(first.startswith('<goldfish-memory>'))
        self.assertLessEqual(len(first), 500)
        for _ in range(5):
            self.assertEqual(self.db.recall('如何聊天', context=first)['injection'], '')
        self.assertTrue(self.db.recall('如何聊天', context='压缩后细节不见了')['injection'])

    def test_character_budget_includes_wrappers_and_escaped_text(self):
        mid = self.db.add_message('s', 'user', '我喜欢咖啡 & 茶')
        self.db.add_summary('用户喜欢咖啡 & 茶', [mid])
        for budget in (80, 200, 300, 500):
            injection = self.db.recall('咖啡', max_chars=budget)['injection']
            self.assertLessEqual(len(injection), budget)

    def test_unversioned_database_migration_preserves_sources(self):
        mid = self.db.add_message('s', 'user', '原始来源')
        self.db.db.execute('PRAGMA user_version=0')
        self.db.close()
        self.db = MemoryDB(self.path)
        self.assertEqual(self.db.db.execute('PRAGMA user_version').fetchone()[0], 3)
        self.assertEqual(self.db.get_message(mid)['text'], '原始来源')
        self.db.close()
        self.db = MemoryDB(self.path)  # Opening the migrated DB is idempotent.
        self.assertEqual(self.db.get_message(mid)['text'], '原始来源')

    def test_unknown_newer_schema_refuses_without_downgrade(self):
        self.db.db.execute('PRAGMA user_version=9')
        with self.assertRaises(ValueError):
            MemoryDB(self.path)
        self.assertEqual(self.db.db.execute('PRAGMA user_version').fetchone()[0], 9)


if __name__ == '__main__':
    unittest.main()
