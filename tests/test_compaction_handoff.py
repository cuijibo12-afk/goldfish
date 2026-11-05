import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from goldfish.engine import MemoryDB
from goldfish import compaction as d, remember
from goldfish import queries
from goldfish.server import Service
from scripts.rollback_d_clone import clone


def source(text='D验收虚构：2024年在松溪站忘带蓝色雨衣后借了黄色雨披。', seq=2):
    return {'namespace': 'test', 'session_id': 'session', 'event_seq': seq, 'message_id': 'u'+str(seq),
            'event_time': '2026-11-05T00:00:00Z', 'role': 'user', 'source_kind': 'user', 'text': text}


def manifest(src=None, cid='c1'):
    src = src or source()
    return {'version': 1, 'authority': 'host_compaction_opt_in/v1', 'namespace': 'test', 'session_id': 'session',
            'compaction_id': cid, 'target': {'provider': 'same', 'model': 'main'},
            'sources': [{'source': src, 'start': 0, 'end': len(src['text'])}]}


def plan(quote=None):
    quote = quote or source()['text']
    return {'summaries': [{'ref': 'n1', 'text': quote, 'quote': quote, 'evidence_ids': ['u0'], 'theme': '出行准备',
                          'start_time': '2024', 'end_time': None}], 'links': [], 'profiles': []}


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.path = Path(self.root.name)/'memory.sqlite3'
        self.db = MemoryDB(self.path)
        d.migrate(self.db)

    def tearDown(self):
        self.db.close()
        self.root.cleanup()

    def ready(self, m=None):
        job = d.enqueue(self.db, m or manifest())['job_id']
        return job, d.prepare(self.db, job)

    def finish(self, job, prepared, result=None):
        return d.commit(self.db, job, result or plan(), {'model_calls': 1}, prepared['snapshot'])

    def test_atomic_receipt_replay_and_overlap(self):
        job, task = self.ready()
        first = self.finish(job, task)
        self.assertEqual(first, {**d.commit(self.db, job, {}, {}, ''), 'replayed': False})
        self.assertTrue(d.enqueue(self.db, manifest())['duplicate'])
        job2, task2 = self.ready(manifest(cid='c2'))
        self.assertEqual(task2['source_count'], 0)
        d.commit(self.db, job2, {'summaries': [], 'profiles': [], 'links': []}, {}, task2['snapshot'])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 1)
        self.assertEqual(d._job(self.db, job)['payload'], '{}')

    def test_transaction_rolls_back_partial_summary_and_progress(self):
        job, task = self.ready()
        self.db.db.execute("CREATE TRIGGER fail_d BEFORE INSERT ON compact_sources BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.finish(job, task)
        for table in ('summaries', 'raw_messages', 'compact_claims', 'compact_sources'):
            self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0], 0)
        self.db.db.execute('DROP TRIGGER fail_d')
        self.finish(job, task)

    def test_restart_resumes_running_task(self):
        job, _ = self.ready()
        self.db.close()
        self.db = MemoryDB(self.path)
        self.assertEqual(self.db.db.execute('PRAGMA user_version').fetchone()[0], 5)
        self.assertEqual(d.handle(self.db, {'action': 'pending'})['jobs'][0]['id'], job)
        self.finish(job, d.prepare(self.db, job))

    def test_b2_saved_fragment_masked_other_fact_remains(self):
        quote = '我在青石站借了黄色雨披。'
        src = source('请记住：'+quote+'后来在2025年买了灰色雨衣。')
        remember.save(self.db, {'version': 1, 'authorization': src, 'source': src,
                               'quote': quote, 'request_quote': '请记住'})
        job, task = self.ready(manifest(src))
        self.assertNotIn(quote, task['context']['messages'][0]['text'])
        later = '后来在2025年买了灰色雨衣。'
        self.finish(job, task, plan(later))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 2)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM explicit_memory_receipts').fetchone()[0], 1)

    def test_concurrent_b2_invalidates_snapshot(self):
        src = source('请记住：我在松溪站借了雨披。')
        job, task = self.ready(manifest(src))
        remember.save(self.db, {'version': 1, 'authorization': src, 'source': src,
                               'quote': '我在松溪站借了雨披。', 'request_quote': '请记住'})
        with self.assertRaisesRegex(ValueError, 'stale_compaction'):
            self.finish(job, task, plan('我在松溪站借了雨披。'))

    def test_assistant_spoof_and_identity_mutation_rejected(self):
        bad = manifest(); bad['sources'][0]['source']['source_kind'] = 'compact-checkpoint'
        with self.assertRaises(ValueError): d.enqueue(self.db, bad)
        self.ready()
        bad = manifest(); bad['sources'][0]['source']['text'] = 'x'*len(source()['text'])
        with self.assertRaises(ValueError): d.enqueue(self.db, bad)

    def test_unobserved_quote_relationship_and_inferred_profile_rejected(self):
        job, task = self.ready()
        for invalid in [plan('助手猜测用户一定害怕承担责任'),
                        {**plan(), 'links': [{'kind': 'possible_cause'}]},
                        {'summaries': [], 'links': [], 'profiles': [{'kind': 'ai_observed', 'text': '猜测',
                          'quote': source()['text'], 'evidence_ids': ['u0']}]}]:
            with self.assertRaises(ValueError): self.finish(job, task, invalid)
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM summaries').fetchone()[0], 0)

    def test_migration_backup_and_old_rows_untouched(self):
        backups = list(self.path.parent.glob('*.before-D-*'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as old:
            self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0], 4)
        d.migrate(self.db)
        self.assertEqual(len(list(self.path.parent.glob('*.before-D-*'))), 1)
        readonly = MemoryDB(self.path, read_only=True)
        self.assertEqual(readonly.db.execute('PRAGMA query_only').fetchone()[0], 1)
        readonly.close()

    def test_disabled_and_unsigned_routes_refuse(self):
        service = Service(self.path, read_only=True, allow_remember=True)
        try:
            with self.assertRaises(PermissionError): service.compaction_signed(b'{}', '')
        finally: service.close()
        service = Service(self.path, read_only=True, allow_compaction=True)
        try:
            self.assertFalse(service.chat.configured)
            self.assertFalse(service.embeddings.configured)
            with self.assertRaises(PermissionError): service.compaction_signed(b'{}', '')
        finally: service.close()

    def test_empty_selection_commits_no_raw_text(self):
        m = manifest(); m['sources'] = []
        job, task = self.ready(m)
        d.commit(self.db, job, {'summaries': [], 'profiles': [], 'links': []}, {}, task['snapshot'])
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM raw_messages').fetchone()[0], 0)

    def test_source_only_identifier_is_searchable_after_lossy_summary(self):
        src = source('D验收虚构：我在松溪-V28修理站借了六角扳手，借条R46放在木盒B17第三格。')
        job, task = self.ready(manifest(src))
        result = plan(src['text']); result['summaries'][0]['text'] = 'D验收虚构：用户借用工具后归还。'
        self.finish(job, task, result)
        before = self.db.db.total_changes
        records = queries.search(self.db, {'query': 'R46', 'limit': 1})['records']
        self.assertEqual(len(records), 1)
        self.assertIn('木盒B17第三格', records[0]['source_excerpt']['text'])
        self.assertEqual(records[0]['provenance'], 'dsh_compaction_user_statement')
        self.assertEqual(self.db.db.total_changes, before)

    def test_rollback_copy_preserves_new_memories_and_b2_receipts(self):
        job, task = self.ready()
        self.finish(job, task)
        src = source('请记住：D验收虚构：我在松溪站借过雨披。', 12)
        remember.save(self.db, {'version': 1, 'authorization': src, 'source': src,
                               'quote': 'D验收虚构：我在松溪站借过雨披。', 'request_quote': '请记住'})
        output = self.path.with_name('rollback.sqlite3')
        clone(self.path, output)
        with closing(sqlite3.connect(output)) as old:
            self.assertEqual(old.execute('PRAGMA user_version').fetchone()[0], 4)
            for table in ('summaries', 'raw_messages', 'evidence', 'explicit_memory_receipts'):
                self.assertEqual(set(old.execute('SELECT * FROM '+table)), set(map(tuple,self.db.db.execute('SELECT * FROM '+table))))
        self.assertEqual(self.db.db.execute('PRAGMA user_version').fetchone()[0], 5)
        with self.assertRaises(ValueError): clone(self.path, self.path)
        with self.assertRaises(ValueError): clone(self.path, output)

    def test_native_success_and_organization_failure_are_separate(self):
        job, task = self.ready()
        d.handle(self.db, {'action': 'fail', 'job_id': job, 'error': 'invalid_result'})
        state = d.handle(self.db, {'action': 'status'})['jobs'][0]
        self.assertEqual(state['native_compaction'], 'succeeded')
        self.assertEqual(state['state'], 'retry')
        self.assertEqual(state['last_error'], 'invalid_result')
        self.assertIsNone(state['receipt'])
        self.assertEqual(d.handle(self.db, {'action': 'pending'})['jobs'], [])
        self.assertEqual(len(d._available(self.db, manifest())), 1)
        result = self.finish(job, task)
        self.assertEqual(result['receipt']['native_compaction'], 'succeeded')


if __name__ == '__main__': unittest.main()
