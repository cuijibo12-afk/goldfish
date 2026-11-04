"""Evaluate real configurable model extraction; never substitute a fake response.

GOLDFISH_LLM_* selects Ollama or an OpenAI-compatible cloud/local endpoint.
The fixture is fictional, pre-registered Chinese dialogue, not personal history.
"""
from __future__ import annotations
import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import time

from goldfish.consolidate import PROMPT, consolidate_session
from goldfish.engine import MemoryDB
from goldfish.providers import ChatModel, EmbeddingModel, load_config
from .scoring import score_run

ROOT = Path(__file__).resolve().parents[1]


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def latency(values):
    ordered = sorted(values)
    return {'count': len(values), 'total_ms': round(sum(values), 3),
            'p50_ms': round(statistics.median(values), 3) if values else None,
            'p95_ms': round(ordered[max(0, math.ceil(len(values) * .95) - 1)], 3) if values else None}


def export_database(db):
    summaries = [dict(row) for row in db.db.execute('SELECT * FROM summaries ORDER BY created_at,id')]
    for summary in summaries:
        summary['evidence_ids'] = [row['id'] for row in db.evidence_for(summary['id'])]
    profiles = [dict(row) for row in db.db.execute('SELECT * FROM profile ORDER BY created_at,id')]
    for profile in profiles:
        profile['evidence_ids'] = json.loads(profile['evidence_ids'])
    links = [dict(row) for row in db.db.execute('SELECT * FROM links ORDER BY created_at,id')]
    for link in links:
        link['evidence_ids'] = [row['id'] for row in db.link_sources(link['id'])]
    themes = [dict(row) for row in db.db.execute('SELECT * FROM themes ORDER BY title')]
    members = [dict(row) for row in db.db.execute('SELECT * FROM theme_members')]
    return {'summaries': summaries, 'profiles': profiles, 'links': links,
            'themes': themes, 'theme_members': members}


class RecordingModel(ChatModel):
    def __init__(self, config, max_calls):
        super().__init__(config)
        self.max_calls = max_calls
        self.calls = []

    def decide(self, prompt, data, **kwargs):
        if len(self.calls)>=self.max_calls: raise RuntimeError('evaluation_model_call_budget_exhausted')
        record={'purpose':kwargs.get('purpose'),'input':data}
        try:
            output,measurement=super().decide(prompt,data,**kwargs)
            record.update({'output':output,'measurement':measurement})
            return output,measurement
        finally:
            if 'measurement' not in record: record['measurement']=self.last_call
            self.calls.append(record)

    def extract(self, messages, related):
        if len(self.calls) >= self.max_calls:
            raise RuntimeError('evaluation_model_call_budget_exhausted')
        record = {'message_ids': [message['id'] for message in messages],
                  'candidate_existing_summaries': related}
        self.last_call = None
        try:
            result = super().extract(messages, related)
            record['output'] = result
            return result
        except Exception as error:
            record['error_type'] = type(error).__name__
            raise
        finally:
            record['measurement'] = self.last_call
            self.calls.append(record)


def evaluate(args):
    config = load_config('LLM')
    if not args.allow_model_calls:
        raise SystemExit('Pass --allow-model-calls to authorize the selected real provider. No model called.')
    if not config.configured:
        raise SystemExit('Configure GOLDFISH_LLM_PROVIDER / MODEL / URL (cloud). No fake model fallback.')
    fixture_path = Path(args.corpus)
    corpus_bytes = fixture_path.read_bytes()
    corpus = json.loads(corpus_bytes)
    output = Path(args.output or ROOT / 'artifacts' / 'real-memory' /
                  (args.label + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')))
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    (output / 'extraction_prompt.txt').write_text(PROMPT)
    dump(output / 'corpus.json', corpus)
    source_digest = hashlib.sha256()
    for name in ('goldfish/engine.py', 'goldfish/consolidate.py', 'goldfish/providers.py', 'evals/scoring.py', 'evals/run_real.py'):
        source_digest.update(name.encode())
        source_digest.update((ROOT / name).read_bytes())
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    model = RecordingModel(config, args.max_calls)
    db = MemoryDB(output / 'memory.sqlite3')
    failures = []
    consolidation = []
    started = time.perf_counter()
    try:
        for session in corpus['sessions']:
            sid = session.get('session_id', session.get('id'))
            for message in session['messages']:
                db.add_message(sid, message['role'], message['text'], message['event_time'],
                               message['id'], message.get('source', 'human' if message['role'] == 'user' else 'assistant_observed'))
            while db.get_unprocessed_messages(sid):
                call_index = len(model.calls)
                try:
                    result = consolidate_session(db, sid, model)
                    consolidation.append({'session_id': sid, **result})
                    if not result.get('processed'): break
                except Exception as error:
                    # Evaluation failures remain visible; no auto-retry hides an
                    # invalid extraction or repeatedly charges the same batch.
                    failures.append({'session_id': sid, 'call_index': call_index,
                                     'error_type': type(error).__name__,
                                     'error_detail': str(error).replace(config.key, '[REDACTED]')[:300] if config.key else str(error)[:300]})
                    print(json.dumps({'session_id': sid, 'failed': type(error).__name__}), flush=True)
                    break
                finally:
                    dump(output / 'model_calls.json', model.calls)
            print(json.dumps({'session_id': sid, 'calls': len(model.calls),
                              'remaining': len(db.get_unprocessed_messages(sid))}), flush=True)
        embeddings = EmbeddingModel()
        embedding_status = {'configured': embeddings.configured, 'enabled_for_run': args.with_embedding}
        if args.with_embedding:
            if not embeddings.configured:
                raise ValueError('--with-embedding requires explicit GOLDFISH_EMBED_* configuration')
            while missing := db.missing_embeddings(embeddings.identity):
                vectors = embeddings.embed([row['text'] for row in missing])
                db.store_embeddings(embeddings.identity, [(row['id'], vector) for row, vector in zip(missing, vectors)])
        exported = export_database(db)
        dump(output / 'memory.json', exported)
        recalls = {}
        graph_off = {}
        recall_ms = []
        dedup = []
        for query in corpus['queries']:
            semantic = None
            if args.with_embedding:
                try: semantic = db.semantic_scores(embeddings.embed([query['text']])[0], embeddings.identity)
                except Exception as error:
                    failures.append({'query_id': query['query_id'], 'stage': 'embedding', 'error_type': type(error).__name__})
            today = date.fromisoformat(query.get('as_of', corpus['metadata'].get('as_of', '2026-11-04'))[:10])
            context = query.get('context', '')
            if not isinstance(context, str): context = ''
            call_start = time.perf_counter()
            result = db.recall(query['text'], context=context, max_items=args.limit,
                               max_chars=args.max_chars, depth=2, today=today, semantic_scores=semantic)
            elapsed = (time.perf_counter() - call_start) * 1000
            recall_ms.append(elapsed)
            recalls[query['query_id']] = {**result, 'latency_ms': round(elapsed, 3)}
            graph_off[query['query_id']] = db.recall(query['text'], context=context, max_items=args.limit,
                max_chars=args.max_chars, depth=0, today=today, semantic_scores=semantic)
            if result['injection']:
                repeat = db.recall(query['text'], context=context + '\n' + result['injection'],
                                  max_items=args.limit, max_chars=args.max_chars, depth=2, today=today, semantic_scores=semantic)
                lost = db.recall(query['text'], context='压缩后仅剩笼统主题，细节已丢失。',
                                max_items=args.limit, max_chars=args.max_chars, depth=2, today=today, semantic_scores=semantic)
                dedup.append({'query_id': query['query_id'], 'initial_ids': [row['id'] for row in result['results']],
                              'repeat_ids': [row['id'] for row in repeat['results']],
                              'recalled_after_loss_ids': [row['id'] for row in lost['results']],
                              'note': 'engine-level text coverage probe; actual DSH compaction is tested separately'})
        dump(output / 'recalls.json', {'graph_on': recalls, 'graph_off': graph_off, 'coverage_probes': dedup})
        scores = score_run(corpus, exported['summaries'], exported['profiles'], exported['links'], recalls)
        scores_off = score_run(corpus, exported['summaries'], exported['profiles'], exported['links'], graph_off)
        measurements = [call['measurement'] for call in model.calls if call.get('measurement')]
        usage = {}
        for measurement in measurements:
            for key, value in (measurement.get('usage') or {}).items():
                if type(value) is int: usage[key] = usage.get(key, 0) + value
        report = {'label': args.label, 'created_at': datetime.now(timezone.utc).isoformat(),
                  'git_commit': commit, 'source_sha256': source_digest.hexdigest(),
                  'fixture_sha256': hashlib.sha256(corpus_bytes).hexdigest(),
                  'fixture_origin': corpus['metadata'], 'model': config.public(), 'embedding': embedding_status,
                  'runtime': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version},
                  'settings': {'limit': args.limit, 'max_chars': args.max_chars, 'depth': 2, 'max_model_calls': args.max_calls},
                  'elapsed_s': round(time.perf_counter() - started, 3), 'model_usage_actual': usage,
                  'model_latency': latency([call['latency_ms'] for call in measurements]),
                  'recall_latency': latency(recall_ms), 'consolidation': consolidation, 'failures': failures,
                  'counts': {key: len(value) for key, value in exported.items()}, 'scores': scores,
                  'graph_off_scores': scores_off,
                  'limitations': ['Constructed fictional Chinese dialogue; real model outputs, not personal conversation logs.',
                      'Keyword/detail probes and evidence IDs are auditable proxies; ambiguous outputs require human review.',
                      'Provider usage is actual; injection length is characters, not an estimated token count.',
                      'No automatic substitution to another model or provider.']}
        dump(output / 'report.json', report)
        dump(output / 'corpus.json', corpus)
        print(json.dumps({'report': str(output / 'report.json'), 'counts': report['counts'],
                          'actual_usage': usage, 'failures': len(failures)}, ensure_ascii=False), flush=True)
        return report
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', default=str(ROOT / 'evals' / 'chinese_memory_cases.json'))
    parser.add_argument('--output')
    parser.add_argument('--label', default='real')
    parser.add_argument('--max-calls', type=int, default=24)
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--max-chars', type=int, default=1800)
    parser.add_argument('--with-embedding', action='store_true')
    parser.add_argument('--allow-model-calls', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.max_calls <= 64: parser.error('--max-calls must be between 1 and 64')
    evaluate(args)


if __name__ == '__main__': main()
