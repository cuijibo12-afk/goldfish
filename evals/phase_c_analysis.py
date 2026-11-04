"""Recompute phase C comparisons offline, without keys or model judgments."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import statistics

from goldfish.engine import _covered
from .scoring import score_run

PROJECT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text())


def analyze(root):
    result = {'provenance': 'Frozen evidence/phrase rules; neither model self-ratings nor human adjudication.',
              'datasets': {}, 'usage_ledger': [], 'unresolved_queue': []}
    for dataset, fixture in [('regression', 'evals/chinese_memory_cases.baseline.json'),
                             ('heldout-once', 'evals/holdout/chinese_sealed_v1.json')]:
        report = read(root / dataset / 'report.json')
        corpus = read(PROJECT / fixture)
        memory = report['memory']
        item = {'fixture_sha256': report['fixture_sha256'], 'depths': {},
                'recorded_core_hashes_match_worktree': all(
                    hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() == sha
                    for name, sha in report['source_hashes'].items()),
                'depth1_depth2_identical_model_inputs': sum(
                    report['after']['1'][qid].get('plan', {}).get('payload') ==
                    report['after']['2'][qid].get('plan', {}).get('payload')
                    for qid in report['after']['1'])}
        for depth in ('0', '1', '2'):
            before = score_run(corpus, memory['summaries'], memory['profiles'], memory['links'], report['before'][depth])
            after = report['scores'][depth]
            rows = list(report['after'][depth].values())
            times = sorted(row['latency_ms'] for row in rows if 'latency_ms' in row)
            item['depths'][depth] = {'before': before['recall']['summary'], 'after': after['recall']['summary'],
                'actual_ranking_usage': {key: sum(row.get('plan', {}).get('measurement', {}).get('usage', {}).get(key, 0)
                    for row in rows) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
                'latency_ms': {'median': statistics.median(times), 'p95_nearest_rank': times[math.ceil(len(times) * .95) - 1], 'max': max(times)},
                'injection_characters_not_tokens': sum(len(row['injection']) for row in rows),
                'model_error_count': sum('error_type' in row for row in rows)}
            for q in after['recall']['queries']:
                for key in ('unknown_ids', 'unresolved_fact_ids', 'no_candidate_fact_ids', 'false_positive_ids'):
                    if q.get(key):
                        result['unresolved_queue'].append({'dataset': dataset, 'depth': depth,
                            'query_id': q['query_id'], 'category': key, 'ids': q[key]})
        item['extraction'] = {k: report['scores']['0']['extraction'][k] for k in
            ('gold_count', 'record_count', 'probe_matched_count', 'manual_review_ids', 'no_evidence_candidate_ids')}
        item['extraction_failures'] = report.get('extraction_failures', [])
        if 'relation_reviews' in report:
            item['model_review_statuses_not_human'] = dict(Counter(row['status'] for row in report['relation_reviews']))
        if 'coverage' in report:
            cases = {c['case_id']: c for c in corpus['coverage_cases']}
            item['coverage_rule_outcomes'] = dict(Counter(c['rule_outcome'] for c in report['coverage']))
            item['coverage_actual_states'] = dict(Counter(c.get('result', {}).get('state', 'error') for c in report['coverage']))
            item['coverage_missing_detail_checks'] = [{
                'case_id': row['case_id'], 'all_expected_fragments_present': all(
                    term in row.get('result', {}).get('text', '') for term in cases[row['case_id']]['missing_contains'])}
                for row in report['coverage'] if cases[row['case_id']]['missing_contains']]
            item['before_exact_text_suppressed_case_ids'] = [case['case_id'] for case in cases.values()
                if _covered(case['memory'], case['context'])]
            item['relation_rule_outcomes_not_human'] = dict(Counter(c['rule_outcome'] for c in report['relations']))
        result['datasets'][dataset] = item

    for name in ('dev', 'dev2', 'regression', 'heldout-once', 'token-probe'):
        report = read(root / name / 'report.json')
        result['usage_ledger'].append({'run': name, 'calls': report.get('calls') if isinstance(report.get('calls'), int) else len(report['calls']),
                                       'usage': report['usage_actual']})
    result['live'] = {}
    for name in ('live-native', 'live-lossy'):
        report = read(root / name / 'report.json')
        usage = {key: sum(row.get('response', {}).get('usage', {}).get(key, 0) for row in report['calls'])
                 for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        result['usage_ledger'].append({'run': name, 'calls': len(report['calls']), 'usage': usage})
        result['live'][name] = {'status': report['status'], 'failed_checks': [c['name'] for c in report['checks'] if not c['passed']],
            'injections_per_turn': [turn['added_memory_messages'] for turn in report['turns']],
            'semantic_fallbacks': [row['response'].get('semantic_fallback') for row in report['recall_calls']],
            'recall_latency_ms': [row['latency_ms'] for row in report['recall_calls']],
            'final_answer': report['turns'][-1]['actual_assistant_text']}
    result['usage_total'] = {key: sum(row['usage'].get(key, 0) for row in result['usage_ledger'])
                            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
    result['calls_total'] = sum(row['calls'] for row in result['usage_ledger'])
    return result


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(analyze(args.root), ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
