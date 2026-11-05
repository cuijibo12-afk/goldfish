"""D: deterministic, signed compaction handoff. No model/provider is instantiated."""
import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from contextlib import closing
from .engine import now_iso
from .remember import _source, source_id
from .consolidate import _validate
from . import events as event_memory

SAFE_VALIDATION_CODES=frozenset({
 'E_plan_and_main_model_review_required','invalid_event_plan','event_plan_too_large','duplicate_event_ref',
 'unobserved_existing_event','legacy_memory_not_an_event_merge_target','event_identity_changed',
 'specific_identity_or_explicit_reference_required','event_key_must_be_literal_user_anchor',
 'existing_event_identity_requires_merge_decision','invalid_event_facts','duplicate_fact_ref',
 'semantic_novelty_classification_required','equivalent_fact_target_required','invalid_fact_aspect',
 'invalid_fact_operation','prior_fact_required','cross_event_fact_merge','explicit_correction_required',
 'inferred_feeling_not_user_expression','invalid_fact_time_precision','event_view_budget_exceeded',
 'relation_ref_collides_with_visible_existing','invalid_relation_endpoints','unconfirmed_causality_not_allowed',
 'relation_review_required','relation_fact_wrong_endpoint','unknown_relation_to_revoke','explicit_relationship_denial_required',
 'fact_evidence_required','quote_not_in_visible_user_source','not_user_asserted_fact','original_quote_missing',
 'new_human_evidence_required','missing_semantic_review','semantic_review_not_supported_facts',
 'semantic_review_not_supported_events','semantic_review_not_supported_revoke','event_context_budget_exceeded',
 'stale_compaction_snapshot_retry'})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def migrate(db):
    """Optional additive v5 migration. Existing v4 use without D stays v4."""
    if db.db.execute('PRAGMA user_version').fetchone()[0] >= 5:
        return
    if db.path != ':memory:':
        backup = db.path + '.before-D-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        with closing(sqlite3.connect(backup)) as target:
            db.db.backup(target)
    with db.transaction():
        db.db.execute('''CREATE TABLE compact_jobs(
          id TEXT PRIMARY KEY, namespace TEXT NOT NULL, session_id TEXT NOT NULL,
          compaction_id TEXT NOT NULL, manifest_hash TEXT NOT NULL, payload TEXT NOT NULL,
          state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
          last_error TEXT NOT NULL DEFAULT '', receipt TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        db.db.execute('''CREATE TABLE compact_sources(
          segment_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_hash TEXT NOT NULL,
          start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, job_id TEXT NOT NULL REFERENCES compact_jobs(id),
          processed_at TEXT NOT NULL)''')
        db.db.execute('''CREATE TABLE compact_claims(
          claim_key TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES raw_messages(id),
          quote TEXT NOT NULL, memory_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES compact_jobs(id),
          kind TEXT NOT NULL, created_at TEXT NOT NULL)''')
        db.db.execute('CREATE INDEX compact_jobs_pending ON compact_jobs(state,next_retry)')
        db.db.execute('''CREATE TABLE compact_attempts(
          attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES compact_jobs(id),
          state TEXT NOT NULL, metrics TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
        db.db.execute('PRAGMA user_version=5')


def segments(manifest):
    if manifest.get('version') != 1 or manifest.get('authority') != 'host_compaction_opt_in/v1':
        raise ValueError('host_compaction_authority_required')
    for key in ('namespace', 'session_id', 'compaction_id'):
        if not isinstance(manifest.get(key), str) or not 0 < len(manifest[key]) <= 200:
            raise ValueError('invalid_compaction_identity')
    if not isinstance(manifest.get('target'), dict) or any(
            not isinstance(manifest['target'].get(k), str) or not manifest['target'][k] for k in ('provider', 'model')):
        raise ValueError('original_main_model_required')
    if not isinstance(manifest.get('sources'), list) or not len(manifest['sources']) <= 4:
        raise ValueError('invalid_compaction_batch')
    output = []
    for item in manifest['sources']:
        source = _source(item.get('source'))
        if any(source[k] != manifest[k] for k in ('namespace', 'session_id')):
            raise ValueError('cross_session_compaction_source')
        start, end = item.get('start'), item.get('end')
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source['text']) or end-start > 6000:
            raise ValueError('invalid_source_segment')
        # Fixed partitioning prevents alternate overlap boundaries on replay.
        if start % 6000 or end != min(start + 6000, len(source['text'])):
            raise ValueError('noncanonical_source_segment')
        sid = source_id(source)
        output.append({**item, 'source_id': sid, 'segment_id': digest([sid, start, end]),
                       'source_hash': digest(source)})
    if sum(s['end']-s['start'] for s in output) > 12000:
        raise ValueError('batch_too_large')
    return output


def enqueue(db, manifest):
    parts = segments(manifest)
    ident = 'compact:' + digest([manifest['namespace'], manifest['session_id'], manifest['compaction_id'],
                                 [p['segment_id'] for p in parts]])
    fingerprint = digest(manifest)
    with db.transaction():
        old = db.db.execute('SELECT * FROM compact_jobs WHERE id=?', (ident,)).fetchone()
        if old:
            if old['manifest_hash'] != fingerprint:
                raise ValueError('immutable_compaction_manifest_changed')
            return {'ok': True, 'job_id': ident, 'state': old['state'], 'duplicate': True}
        stamp = now_iso()
        db.db.execute('''INSERT INTO compact_jobs(id,namespace,session_id,compaction_id,manifest_hash,payload,state,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?)''', (ident, manifest['namespace'], manifest['session_id'],
                        manifest['compaction_id'], fingerprint, json.dumps(manifest, ensure_ascii=False), 'pending', stamp, stamp))
    return {'ok': True, 'job_id': ident, 'state': 'pending', 'duplicate': False}


def _job(db, ident):
    row = db.db.execute('SELECT * FROM compact_jobs WHERE id=?', (ident,)).fetchone()
    if not row:
        raise ValueError('unknown_compaction_job')
    return dict(row)


def _saved_quotes(db, sid):
    return [r[0] for r in db.db.execute('SELECT quote FROM explicit_memory_claims WHERE source_message_id=?', (sid,))] + [
        r[0] for r in db.db.execute('SELECT quote FROM compact_claims WHERE source_id=?', (sid,))]


def _covered(quote, saved):
    # Containment, not semantic similarity: never merge different dated experiences.
    normalize = lambda s: ''.join(s.split())
    return any(normalize(quote) in normalize(q) for q in saved)


def _available(db, manifest):
    out = []
    for part in segments(manifest):
        done = db.db.execute('SELECT source_hash FROM compact_sources WHERE segment_id=?', (part['segment_id'],)).fetchone()
        if done:
            if done[0] != part['source_hash']:
                raise ValueError('immutable_source_changed')
            continue
        text = part['source']['text']
        # The model never sees the already-saved fragment as a fresh candidate.
        for quote in _saved_quotes(db, part['source_id']):
            text = text.replace(quote, ' ' * len(quote))
        out.append({**part, 'available_text': text[part['start']:part['end']]})
    return out


def _context(db, parts, candidates):
    if event_memory.enabled(db):
        return event_memory.context(db,parts,candidates)
    return {'messages': [{'id': 'u'+str(i), 'text': p['available_text'], 'role': 'user',
                    'event_time': p['source']['event_time']} for i, p in enumerate(parts)],
            'existing': [{'id': 'm'+str(i), 'text': r['text'], 'start_time': r['start_time'],
                    'end_time': r['end_time'], 'themes': r.get('theme_titles')} for i, r in enumerate(candidates)]}, None


def prepare(db, ident):
    with db.transaction():
        row = _job(db, ident)
        if row['state'] == 'done':
            return {'ok': True, 'state': 'done', 'receipt': json.loads(row['receipt'])}
        manifest = json.loads(row['payload'])
        parts = _available(db, manifest)
        candidates = db.recent_summaries_for_extraction(' '.join(p['available_text'] for p in parts), limit=12)
        # Compact aliases avoid model copying long canonical IDs; server owns mapping.
        context, _ = _context(db,parts,candidates)
        db.db.execute("UPDATE compact_jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?", (now_iso(), ident))
        return {'ok': True, 'state': 'running', 'job_id': ident, 'target': manifest['target'],
                'session_id': manifest['session_id'], 'context': context,
                'snapshot': digest(context), 'source_count': len(parts),
                'format':'goldfish_events/v1' if event_memory.enabled(db) else 'goldfish_quotes/v1'}


def commit(db, ident, result, metrics, snapshot):
    """Validate before storing; commit all memory effects and processing receipt together."""
    with db.transaction():
        job = _job(db, ident)
        if job['state'] == 'done':
            return {'ok': True, 'state': 'done', 'receipt': json.loads(job['receipt']), 'replayed': True}
        manifest = json.loads(job['payload'])
        parts = _available(db, manifest)
        # Snapshot required: concurrent B2 or overlapping jobs invalidate stale aliases.
        candidates = db.recent_summaries_for_extraction(' '.join(p['available_text'] for p in parts), limit=12)
        context, event_env = _context(db,parts,candidates)
        if digest(context) != snapshot:
            raise ValueError('stale_compaction_snapshot_retry')
        if event_memory.enabled(db):
            saved_ids, duplicate_count = event_memory.apply(db,ident,result,event_env)
        else:
            saved_ids, duplicate_count = _apply_quotes(db,ident,result,parts)
        for part in parts:
            db.db.execute('INSERT INTO compact_sources VALUES(?,?,?,?,?,?,?)',
                          (part['segment_id'], part['source_id'], part['source_hash'], part['start'], part['end'], ident, now_iso()))
        receipt = {'id': 'compact-receipt:' + digest(ident), 'job_id': ident, 'memory_ids': saved_ids,
                   'processed_segments': len(parts), 'duplicates_avoided': duplicate_count,
                   'saved_at': now_iso(), 'metrics': metrics, 'authority': 'host_compaction_opt_in/v1',
                   'verification': 'user_source_grounded_not_independently_verified',
                   'native_compaction': 'succeeded', 'compaction_range': manifest.get('compaction', {})}
        db.db.execute("UPDATE compact_jobs SET state='done',payload='{}',last_error='',receipt=?,updated_at=? WHERE id=?",
                      (json.dumps(receipt, ensure_ascii=False), now_iso(), ident))
        return {'ok': True, 'state': 'done', 'receipt': receipt, 'replayed': False}


def _apply_quotes(db, ident, result, parts):
        if not isinstance(result, dict) or set(result) != {'summaries', 'profiles', 'links'}:
            raise ValueError('invalid_compaction_result')
        # D never promotes an unverified model relationship. Existing graph services
        # remain readable; a future explicit relation confirmation can reuse them.
        if result['links']:
            raise ValueError('unconfirmed_relationship_not_authorized')
        by_alias = {'u'+str(i): p for i, p in enumerate(parts)}
        msg_ids = {k: {'role': 'user'} for k in by_alias}
        _validate(result, msg_ids, [])
        saved_ids, duplicate_count = [], 0
        for kind in ('summaries', 'profiles'):
            for item in result[kind]:
                if len(item['text']) > 2000 or len(item['evidence_ids']) != 1:
                    raise ValueError('one_literal_user_source_per_claim_required')
                alias = item['evidence_ids'][0]
                part = by_alias[alias]
                quote = item.get('quote')
                if not isinstance(quote, str) or not quote.strip() or quote != quote.strip() or len(quote) > 2000 or quote not in part['available_text']:
                    raise ValueError('literal_unconsumed_quote_required')
                # Clear negative directives and questions cannot turn into facts.
                if re.search(r'(?:不要|别|禁止|不必|无需).{0,12}(?:记住|记忆|保存|记录)|[?？]|假设|如果|模型推测|预测', quote):
                    raise ValueError('not_an_asserted_user_experience')
                if kind == 'profiles' and (item['kind'] not in {'user_profile', 'interaction_expectation'}
                        or not re.search(r'长期|一直|今后|以后|每次|始终|习惯|偏好', quote)):
                    raise ValueError('explicit_durable_user_preference_required')
                sid = part['source_id']
                if _covered(quote, _saved_quotes(db, sid)):
                    duplicate_count += 1
                    continue
                source = part['source']
                db.add_message(source['namespace'] + ':' + source['session_id'], 'user', source['text'],
                               source['event_time'], sid, 'human')
                claim = digest([sid, quote, kind])
                # A source quote is audit evidence, never a declaration of independent truth.
                if kind == 'summaries':
                    memory = 'compact-memory:' + claim
                    db.add_summary(item['text'], [sid], item.get('start_time'), item.get('end_time'), summary_id=memory)
                    if item.get('theme', '').strip():
                        if len(item['theme']) > 80:
                            raise ValueError('theme_too_long')
                        db.attach_theme(db.add_theme(item['theme'], item.get('theme_description', '')),
                                        memory, item.get('theme_reason', ''))
                else:
                    memory = 'profile:' + db.add_profile(item['kind'], item['text'], [sid])
                db.db.execute('INSERT INTO compact_claims VALUES(?,?,?,?,?,?,?)',
                              (claim, sid, quote, memory, ident, kind, now_iso()))
                saved_ids.append(memory)
        return saved_ids, duplicate_count


def handle(db, data):
    action = data.get('action')
    if action == 'enqueue':
        return enqueue(db, data['manifest'])
    if action == 'prepare':
        return prepare(db, data['job_id'])
    if action == 'commit':
        return commit(db, data['job_id'], data['result'], data.get('metrics', {}), data['snapshot'])
    if action == 'fail':
        with db.transaction():
            job = _job(db, data['job_id'])
            if job['state'] != 'done':
                delay = min(3600, 15 * 2 ** min(job['attempts'], 8))
                # Store only a fixed safe code, never response text/keys/raw reasoning.
                error = data.get('error', 'organize_failed')
                if error not in SAFE_VALIDATION_CODES and error not in {'organize_failed', 'invalid_result', 'commit_failed', 'cancelled', 'model_unavailable'}:
                    error = error if error in {'specific_identity_or_explicit_reference_required','event_identity_changed',
                      'prior_fact_required','cross_event_fact_merge','semantic_review_not_supported_facts',
                      'semantic_review_not_supported_events','quote_not_in_visible_user_source',
                      'semantic_novelty_classification_required','existing_event_identity_requires_merge_decision',
                      'stale_compaction_snapshot_retry','semantic_validation_rejected','prepare_validation_rejected'} else 'organize_failed'
                held=event_memory.enabled(db) and (error in {'prepare_validation_rejected','event_context_budget_exceeded','event_view_budget_exceeded'} or
                    job['attempts']>=3 and error in (SAFE_VALIDATION_CODES-{'stale_compaction_snapshot_retry'})|{'invalid_result','semantic_validation_rejected'})
                db.db.execute("UPDATE compact_jobs SET state=?,next_retry=?,last_error=?,updated_at=? WHERE id=?",
                              ('needs_review' if held else 'retry',time.time()+delay,error,now_iso(),job['id']))
        return {'ok': True}
    if action=='retry':
        with db.transaction():
            job=_job(db,data['job_id'])
            if job['state']!='done':
                db.db.execute("UPDATE compact_jobs SET state='pending',attempts=0,next_retry=0,last_error='',updated_at=? WHERE id=?",(now_iso(),job['id']))
        return {'ok':True,'job_id':job['id'],'state':'done' if job['state']=='done' else 'pending','scope':'same immutable original source range only'}
    if action == 'attempt':
        with db.transaction():
            _job(db, data['job_id'])
            # Technical metrics only. Never persist discarded model output.
            allowed = {'model_calls', 'elapsed_ms', 'provider', 'model', 'reasoningEffort', 'usage','phase','first_frame_ms'}
            metrics = {k: v for k, v in data.get('metrics', {}).items() if k in allowed}
            db.db.execute('''INSERT INTO compact_attempts VALUES(?,?,?,?,?,?)
              ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state,metrics=excluded.metrics,updated_at=excluded.updated_at''',
              (data['attempt_id'], data['job_id'], data['state'], json.dumps(metrics), now_iso(), now_iso()))
        return {'ok': True}
    if action == 'pending':
        # Running means a prior host may have died. A single host-owned serial
        # worker calls this only when it has no live task.
        rows = db.db.execute("SELECT id,state FROM compact_jobs WHERE state IN ('pending','running','retry') AND next_retry<=? ORDER BY created_at LIMIT 8", (time.time(),))
        return {'ok': True, 'jobs': [dict(r) for r in rows]}
    if action == 'status':
        rows = db.db.execute('SELECT id,session_id,compaction_id,state,attempts,last_error,receipt,created_at,updated_at FROM compact_jobs ORDER BY created_at DESC LIMIT 50')
        return {'ok': True, 'jobs': [{**dict(r), 'native_compaction': 'succeeded',
                 'native_state_basis': 'host_validated_start_summary_checkpoint_end',
                 'receipt': json.loads(r['receipt']) if r['receipt'] else None} for r in rows],
                'scope': 'Only successful native compactions admitted by the host appear here; DSH compression failures remain in the native session log.'}
    raise ValueError('unknown_compaction_action')
