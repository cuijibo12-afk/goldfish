"""Read-only tool contract, with synthetic in-memory sources only."""
import json
import os
import unittest
from unittest.mock import patch
from goldfish.engine import MemoryDB
from goldfish import queries
from goldfish.server import Service


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        self.db = MemoryDB(':memory:')
        mid = self.db.add_message('test', 'user', '2025年3月我把红伞RS-17借给小林。', message_id='test:1')
        self.first = self.db.add_summary('红伞RS-17借给小林', [mid], start_time='2025-03', summary_id='first')
        mid2 = self.db.add_message('test', 'user', '小林去杭州坐火车，需要用这把伞。', message_id='test:2')
        self.second = self.db.add_summary('小林去杭州坐火车', [mid2], summary_id='second')
        theme = self.db.add_theme('杭州出行')
        self.db.attach_theme(theme, self.first)
        self.db.attach_theme(theme, self.second)
        self.db.add_link(self.first, self.second, 'same_event', '同一次出行的借伞与交通安排', origin='human_review', confirmed=True)
        self.profile = self.db.add_profile('interaction_expectation', '我希望交流简洁', [mid])

    def tearDown(self):
        self.db.close()

    def test_search_explore_and_evidence_do_not_write_or_inject(self):
        before = self.db.db.total_changes
        found = queries.search(self.db, {'query': 'RS-17'})
        self.assertEqual(found['records'][0]['id'], self.first)
        self.assertNotIn('injection', found)
        self.assertEqual(found['records'][0]['evidence_refs'][0]['id'], 'test:1')
        related = queries.explore(self.db, {'memory_ids': [self.first]})
        self.assertEqual(related['records'][0]['id'], self.second)
        original = queries.evidence(self.db, {'memory_id': self.first})
        self.assertIn('借给小林', original['records'][0]['text'])
        self.assertEqual(self.db.db.total_changes, before)
        self.assertFalse(found['chat_model_used'])

    def test_explicit_dates_do_not_substitute_source_message_dates(self):
        result = queries.search(self.db, {'query': '小林', 'start_date': '2025-03-10', 'end_date': '2025-03-20'})
        self.assertEqual([r['id'] for r in result['records']], [self.first])
        result = queries.search(self.db, {'query': '小林', 'start_date': '2025-03-10', 'include_undated': True})
        self.assertEqual({r['id'] for r in result['records']}, {self.first, self.second})
        with self.assertRaises(ValueError): queries.search(self.db, {'query': '小林', 'start_date': '2025-02-30'})

    def test_profile_sources_and_evidence_are_addressable(self):
        result = queries.search(self.db, {'query': '交流简洁'})
        self.assertEqual(result['records'][0]['id'], 'profile:' + self.profile)
        original = queries.evidence(self.db, {'memory_id': result['records'][0]['id']})
        self.assertEqual(original['records'][0]['id'], 'test:1')

    def test_pagination_and_stale_cursor(self):
        first = queries.search(self.db, {'query': '小林', 'limit': 1})
        second = queries.search(self.db, {'query': '小林', 'limit': 1, 'cursor': first['next_cursor']})
        self.assertNotEqual(first['records'][0]['id'], second['records'][0]['id'])
        self.db.archive_summary(self.second)
        with self.assertRaisesRegex(ValueError, 'cursor_stale'):
            queries.search(self.db, {'query': '小林', 'cursor': first['next_cursor']})

    def test_full_source_is_paginated_without_text_loss(self):
        text = '长原话\\\"\n' * 3000
        mid = self.db.add_message('long', 'user', text, message_id='long:1')
        sid = self.db.add_summary('长原话', [mid])
        parts, cursor = [], None
        while True:
            request = {'memory_id': sid, 'max_chars': 128}
            if cursor: request['cursor'] = cursor
            result = queries.evidence(self.db, request)
            self.assertLessEqual(sum(len(r['text']) for r in result['records']), 128)
            parts.extend(row['text'] for row in result['records'])
            cursor = result['next_cursor']
            if not cursor: break
        self.assertEqual(''.join(parts), text)

    def test_exploration_is_not_blocked_by_full_direct_candidate_pool(self):
        for index in range(10):
            self.db.add_summary('小林的红伞RS-17相关信息' + str(index), ['test:1'])
        result = queries.explore(self.db, {'memory_ids': [self.first], 'include_candidates': False})
        self.assertEqual([r['id'] for r in result['records']], [self.second])
        self.assertEqual(result['records'][0]['route'], 'stored_relation')

    def test_unreviewed_relation_is_not_returned_as_supported_edge(self):
        sid = self.db.add_summary('其他独立经历', ['test:2'])
        self.db.add_link(self.first, sid, 'possible_cause', '尚未验证的假设')
        result = queries.explore(self.db, {'memory_ids': [self.first], 'include_candidates': False})
        self.assertNotIn(sid, [r['id'] for r in result['records']])

    def test_legacy_direction_is_explicitly_unknown(self):
        self.db.add_link(self.first, self.second, 'temporal_sequence', '旧的先后关系', origin='human_review', confirmed=True)
        result = queries.explore(self.db, {'memory_ids': [self.first], 'relation_kinds': ['temporal_sequence'], 'include_candidates': False})
        self.assertEqual(result['records'][0]['path'][0]['direction'], 'unknown_legacy')
        self.assertFalse(result['records'][0]['relation_is_causality_proof'])

    def test_exclusions_and_discovery_do_not_create_relations(self):
        before = self.db.db.execute('select count(*) from links').fetchone()[0]
        result = queries.explore(self.db, {'memory_ids': [self.first], 'exclude_ids': [self.second], 'query': '杭州'})
        self.assertEqual(result['records'], [])
        self.assertEqual(self.db.db.execute('select count(*) from links').fetchone()[0], before)

    def test_invalid_requests_cannot_become_sql(self):
        for data in ({'query': ''}, {'query': '伞', 'limit': True}, {'query': '伞', 'cursor': 'not-a-cursor'}):
            with self.assertRaises(ValueError): queries.search(self.db, data)
        self.assertEqual(queries.evidence(self.db, {'memory_id': self.first})['source_count'], 1)
        with self.assertRaises(ValueError): queries.evidence(self.db, {'memory_id': "' OR 1=1 --"})
        with self.assertRaises(ValueError): queries.explore(self.db, {'memory_ids': [self.first], 'depth': 3})

    def test_service_never_calls_chat_ranker_even_when_semantic_recall_enabled(self):
        with patch.dict(os.environ, {'GOLDFISH_SEMANTIC_RECALL': '1'}, clear=True):
            service = Service(':memory:')
        class ForbiddenChat:
            configured = True
            def decide(self, *args, **kwargs): raise AssertionError('chat model must not run')
        service.chat = ForbiddenChat()
        try:
            mid = service.db.add_message('s', 'user', '测试伞')
            sid = service.db.add_summary('测试伞', [mid])
            self.assertTrue(service.handle('/search', {'query': '测试伞'})['ok'])
            self.assertTrue(service.handle('/explore', {'memory_ids': [sid]})['ok'])
            self.assertTrue(service.handle('/memory_evidence', {'memory_id': sid})['ok'])
            class FailedEmbedding:
                configured = True
                identity = 'failed-synthetic'
                def embed(self, *args, **kwargs): raise RuntimeError('private provider diagnostics')
            service.embeddings = FailedEmbedding()
            result = service.handle('/search', {'query': '测试伞'})
            self.assertTrue(result['degraded'])
            self.assertEqual(result['records'][0]['id'], sid)
            self.assertNotIn('private provider', json.dumps(result))
        finally: service.close()
