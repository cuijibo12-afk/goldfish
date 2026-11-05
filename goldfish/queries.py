"""Read-only, source-linked memory tools. Never calls a chat model or writes claims."""
import base64
import calendar
from datetime import date
import hashlib
import json
import re
import time
from collections import deque

from .retrieval import candidate_pool, profile_candidates
from .engine import similarity, tokens, QUESTION_TOKENS
from .relations import canonical, KINDS
from . import events, event_queries


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _integer(data, key, default, low, high):
    value = data.get(key, default)
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_' + key)
    return value


def _string(data, key, default='', required=False):
    value = data.get(key, default)
    if not isinstance(value, str) or len(value) > 2000 or (required and not value.strip()):
        raise ValueError('invalid_' + key)
    return value.strip()


def _ids(data, key, maximum=100):
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > maximum or any(not isinstance(v, str) or not v or len(v) > 200 for v in value):
        raise ValueError('invalid_' + key)
    return list(dict.fromkeys(value))


def _boolean(data, key, default):
    value = data.get(key, default)
    if type(value) is not bool:
        raise ValueError('invalid_' + key)
    return value


def _date(text, end=False):
    if not text:
        return None
    try:
        # Existing summaries may have only a year or month. Do not substitute
        # the date on which the source message happened to be recorded.
        value = str(text)[:10]
        if len(value) == 4:
            return date(int(value), 12 if end else 1, 31 if end else 1)
        if len(value) == 7:
            year, month = map(int, value.split('-'))
            return date(year, month, calendar.monthrange(year, month)[1] if end else 1)
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _filters(data):
    start, end = _string(data, 'start_date'), _string(data, 'end_date')
    if any(value and (len(value) != 10 or _date(value) is None) for value in (start, end)):
        raise ValueError('invalid_date_filter')
    if start and end and start > end:
        raise ValueError('invalid_date_range')
    return _date(start), _date(end), _boolean(data, 'include_undated', False), _string(data, 'theme')


def _matches(record, filters):
    start, end, include_undated, theme = filters
    if theme and not any(theme.casefold() in name.casefold() for name in record['themes']):
        return False
    if start or end:
        left = _date(record['event_start'] or record['event_end'])
        right = _date(record['event_end'] or record['event_start'], end=True)
        if left is None or right is None:
            return include_undated
        if (start and right < start) or (end and left > end):
            return False
    return True


def _source_ref(row):
    return {key: row[key] for key in ('id', 'session_id', 'role', 'event_time', 'source')}


def _record(db, memory_id, query=''):
    if memory_id.startswith('profile:'):
        row = db.db.execute("SELECT * FROM profile WHERE id=? AND status='active'", (memory_id[8:],)).fetchone()
        if row is None:
            return None
        row = dict(row)
        sources = [source for mid in json.loads(row['evidence_ids']) if (source := db.get_message(mid))]
        kind, themes = row['kind'], []
    else:
        row = db.db.execute("SELECT * FROM summaries WHERE id=? AND status='active'", (memory_id,)).fetchone()
        if row is None:
            return None
        row = dict(row)
        sources = db.evidence_for(memory_id)
        themes = [r[0] for r in db.db.execute('''SELECT t.title FROM themes t JOIN theme_members tm
            ON tm.theme_id=t.id WHERE tm.summary_id=? ORDER BY t.title''', (memory_id,))]
        kind = 'summary'
    explicit = False
    if kind == 'summary' and db.db.execute('PRAGMA user_version').fetchone()[0] >= 4:
        explicit = db.db.execute('SELECT 1 FROM explicit_memory_claims WHERE summary_id=?', (memory_id,)).fetchone() is not None
    compacted = False
    if db.db.execute('PRAGMA user_version').fetchone()[0] >= 5:
        compacted = db.db.execute('SELECT 1 FROM compact_claims WHERE memory_id=?', (memory_id,)).fetchone() is not None
    record = {'id': memory_id, 'kind': 'explicit_user_statement' if explicit else kind, 'text': row['text'][:2000],
            'text_truncated': len(row['text']) > 2000, 'version': row.get('version', 1),
            'event_start': row.get('start_time'), 'event_end': row.get('end_time'),
            'recorded_at': row['created_at'], 'themes': themes,
            'evidence_refs': [_source_ref(source) for source in sources[:20]],
            'evidence_refs_truncated': len(sources) > 20,
            'evidence_count': len(sources), 'provenance': 'user_requested_quote' if explicit else 'dsh_compaction_user_statement' if compacted else 'source_summary',
            'verification': 'literal_user_quote_not_independently_verified' if explicit else 'source_ids_validated_not_independently_verified'}
    return event_queries.decorate(db,record,query) if events.enabled(db) else record


def _cursor(data, fingerprint, count=2):
    token = data.get('cursor', '')
    if not token:
        return [0] * count
    if not isinstance(token, str) or len(token) > 2000:
        raise ValueError('invalid_cursor')
    try:
        state = json.loads(base64.urlsafe_b64decode(token.encode('ascii')))
        position = state['position']
        if state['fingerprint'] != fingerprint:
            raise ValueError('cursor_stale')
        if not isinstance(position, list) or len(position) != count or any(type(v) is not int or v < 0 for v in position):
            raise ValueError('invalid_cursor')
        return position
    except ValueError as error:
        if str(error) == 'cursor_stale':
            raise
        raise ValueError('invalid_cursor') from None
    except (KeyError, TypeError, UnicodeError):
        raise ValueError('invalid_cursor') from None


def _token(fingerprint, position):
    return base64.urlsafe_b64encode(json.dumps({'fingerprint': fingerprint, 'position': position}).encode()).decode()


def _page(data, records):
    limit = _integer(data, 'limit', 5, 1, 20)
    # Cursor identity includes source-linked results and filters, but no raw
    # query or evidence text is encoded in the cursor itself.
    fingerprint = _digest([records, {k: v for k, v in data.items() if k not in ('cursor', 'limit')}])
    offset = _cursor(data, fingerprint, count=1)[0]
    if offset > len(records):
        raise ValueError('invalid_cursor')
    selected = records[offset:offset + limit]
    next_offset = offset + len(selected)
    return {'ok': True, 'records': selected, 'next_cursor': _token(fingerprint, [next_offset]) if next_offset < len(records) else None,
            'candidate_count': len(records), 'read_only': True, 'chat_model_used': False}


def _compaction_source_candidates(db, query):
    """A lossy summary must not erase a source-only name/code from recall.
    Search only evidence of accepted D memories, not discarded raw job inputs.
    No write, embedding or LLM call; excerpts remain explicitly user quotations.
    """
    if events.enabled(db):
        return event_queries.source_candidates(db,query)
    if db.db.execute('PRAGMA user_version').fetchone()[0] < 5:
        return {}
    result = {}
    query_terms = tokens(query) - QUESTION_TOKENS
    codes = re.findall(r'[a-zA-Z][a-zA-Z0-9_-]*\d[a-zA-Z0-9_-]*', query)
    for row in db.db.execute('''SELECT DISTINCT c.memory_id,r.id,r.text FROM compact_claims c
                               JOIN raw_messages r ON r.id=c.source_id WHERE r.role='user' AND r.source='human' '''):
        shared = query_terms & tokens(row['text'])
        exact = any(code.casefold() in row['text'].casefold() for code in codes)
        lexical = similarity(query, row['text'], True)
        if not exact and (len(shared) < 2 or lexical < .2):
            continue
        score = (.8 if exact else .1) + lexical * .3
        mid = row['memory_id']
        if mid in result and result[mid]['score'] >= score:
            continue
        needles = [code.casefold() for code in codes if code.casefold() in row['text'].casefold()] or sorted(shared, key=lambda s: (-len(s), s))
        offset = max(0, row['text'].casefold().find(needles[0]) - 70) if needles else 0
        excerpt = row['text'][offset:offset+500]
        result[mid] = {'id': mid, 'score': score, 'channels': {'original_user_evidence': lexical, 'exact_source_code': int(exact)},
                       'source_excerpt': {'message_id': row['id'], 'role': 'user', 'text': excerpt,
                                          'text_offset': offset, 'truncated': offset+len(excerpt) < len(row['text']),
                                          'scope': 'original_user_quote_not_new_instruction_or_verified_fact'}}
    return result


def search(db, data, *, semantic=None, auxiliary=None):
    started=time.perf_counter()
    query = _string(data, 'query', required=True)
    filters, excluded = _filters(data), set(_ids(data, 'exclude_ids'))
    with db.lock:
        pool = candidate_pool(db, query, semantic=semantic, auxiliary=auxiliary, mode='hybrid', limit=100)
        ranked_at=time.perf_counter()
        candidates = {r['id']: r for r in pool['summaries'] + profile_candidates(db, query, auxiliary, limit=100)}
        for mid, source in _compaction_source_candidates(db, query).items():
            prior = candidates.get(mid)
            candidates[mid] = {**source, 'score': max(source['score'], prior['score'] if prior else 0),
                               'channels': {**(prior['channels'] if prior else {}), **source['channels']}}
        if events.enabled(db):
            for mid,candidate in candidates.items():
                event=db.db.execute('SELECT identity_key FROM memory_events WHERE id=?',(mid,)).fetchone()
                if event and events.identity_matches(event['identity_key'],query):
                    candidate['score']+=1.2
                    candidate['channels']['exact_event_identity']=1
        indexed_at=time.perf_counter()
        records = []
        for candidate in sorted(candidates.values(), key=lambda r: (-r['score'], r['id'])):
            record = _record(db, candidate['id'],query)
            if record and record['id'] not in excluded and _matches(record, filters):
                record.update(retrieval_score=round(candidate['score'], 6), signals=candidate['channels'])
                if 'source_excerpt' in candidate:
                    record['source_excerpt'] = candidate['source_excerpt']
                records.append(record)
        answer=_page(data, records)
        answer['timings']={'candidate_rank_ms':round((ranked_at-started)*1000,3),
                           'source_lookup_ms':round((indexed_at-ranked_at)*1000,3),
                           'record_render_ms':round((time.perf_counter()-indexed_at)*1000,3),
                           'query_total_ms':round((time.perf_counter()-started)*1000,3)}
        return answer


def explore(db, data, *, semantic=None, auxiliary=None):
    if events.enabled(db):
        return event_queries.explore(db,data,__import__(__name__,fromlist=['']),
          lambda request:_explore_legacy(db,request,semantic=semantic,auxiliary=auxiliary))
    return _explore_legacy(db,data,semantic=semantic,auxiliary=auxiliary)


def _explore_legacy(db, data, *, semantic=None, auxiliary=None):
    seeds = _ids(data, 'memory_ids', maximum=10)
    if not seeds:
        raise ValueError('memory_ids_required')
    depth = _integer(data, 'depth', 1, 1, 2)
    excluded = set(_ids(data, 'exclude_ids')) | set(seeds)
    filters, query = _filters(data), _string(data, 'query')
    kinds = {canonical(kind) for kind in _ids(data, 'relation_kinds', maximum=20)}
    if not kinds.issubset(KINDS | {'reported_influence'}):
        raise ValueError('invalid_relation_kind')
    include_candidates = _boolean(data, 'include_candidates', True)
    with db.lock:
        seed_records = [_record(db, seed) for seed in seeds]
        if any(record is None for record in seed_records):
            raise ValueError('memory_not_found')
        edges = db.active_reviewed_links()
        adjacency = {}
        for edge in sorted(edges, key=lambda e: e['id']):
            kind = canonical(edge['effective_kind'])
            if kinds and kind not in kinds:
                continue
            for left, right in [(edge['left_id'], edge['right_id']), (edge['right_id'], edge['left_id'])]:
                adjacency.setdefault(left, []).append((right, edge))
        found, visited = {}, set(seeds)
        queue = deque((seed, []) for seed in seeds)
        while queue and len(visited) < 200:
            current, path = queue.popleft()
            if len(path) >= depth:
                continue
            for neighbor, edge in adjacency.get(current, []):
                if len(visited) >= 200:
                    break
                if neighbor in visited:
                    continue
                record = _record(db, neighbor)
                if record is None:
                    continue
                visited.add(neighbor)
                kind = canonical(edge['effective_kind'])
                step = {'link_id': edge['id'], 'from': current, 'to': neighbor, 'kind': kind,
                        'reason': edge['review_reason'],
                        'review': 'human_confirmed' if edge['confirmed'] else 'model_quote_reviewed',
                        'direction': 'unknown_legacy' if kind in {'temporal_sequence', 'possible_cause', 'causal_confirmed'} else 'symmetric'}
                route = path + [step]
                record.update(route='stored_relation', path=route, relation_is_causality_proof=False)
                if neighbor not in excluded:
                    found[neighbor] = record
                queue.append((neighbor, route))
        if include_candidates:
            # Theme/shared-source and query matches are discovery leads, never
            # promoted into database relation edges by this read operation.
            sources = {ref['id'] for seed in seed_records for ref in seed['evidence_refs']}
            themes = {theme for seed in seed_records for theme in seed['themes']}
            pool = candidate_pool(db, query, semantic=semantic, auxiliary=auxiliary, mode='hybrid', limit=100) if query else None
            query_scores = {r['id']: r['score'] for r in pool['summaries'] + pool['profiles']} if pool else {}
            if query:
                for mid, candidate in _compaction_source_candidates(db, query).items():
                    query_scores[mid] = max(query_scores.get(mid, 0), candidate['score'])
            for row in db._summary_with_context():
                sid = row['id']
                if sid in found or sid in excluded:
                    continue
                record = _record(db, sid)
                shared_source = bool(sources & {ref['id'] for ref in record['evidence_refs']})
                shared_theme = bool(themes & set(record['themes']))
                if not (shared_source or shared_theme or sid in query_scores):
                    continue
                record.update(route='shared_source' if shared_source else 'theme_candidate' if shared_theme else 'query_candidate',
                              path=[], relation_is_causality_proof=False,
                              retrieval_score=round(query_scores.get(sid, similarity(query, record['text'], True) if query else 0), 6))
                found[sid] = record
                if len(found) >= 200:
                    break
        records = [record for record in found.values() if _matches(record, filters)]
        records.sort(key=lambda r: (r['route'] != 'stored_relation', len(r['path']) if r['path'] else 3,
                                   -r.get('retrieval_score', 0), r['id']))
        result = _page(data, records)
        result['seed_ids'] = seeds
        return result


def evidence(db, data):
    memory_id = _string(data, 'memory_id', required=True)
    budget = _integer(data, 'max_chars', 4000, 128, 8000)
    with db.lock:
        if events.enabled(db) and (memory_id.startswith('relation:') or memory_id.startswith('event:') and not data.get('full_source',False)):
            return event_queries.evidence(db,data,__import__(__name__,fromlist=['']))
        if memory_id.startswith('link:'):
            if not db.db.execute('SELECT 1 FROM links WHERE id=?', (memory_id[5:],)).fetchone():
                raise ValueError('memory_not_found')
            sources = db.link_sources(memory_id[5:])
        else:
            record = _record(db, memory_id)
            if record is None:
                raise ValueError('memory_not_found')
            if memory_id.startswith('profile:'):
                row = db.db.execute('SELECT evidence_ids FROM profile WHERE id=?', (memory_id[8:],)).fetchone()
                sources = [db.get_message(mid) for mid in json.loads(row[0])]
                sources = [source for source in sources if source]
            else:
                sources = db.evidence_for(memory_id)
        if data.get('source_id'):
            sources=[r for r in sources if r['id']==data['source_id']]
            if not sources:raise ValueError('source_not_in_memory')
        if data.get('fact_id') and events.enabled(db) and memory_id.startswith('event:'):
            allowed={r[0] for r in db.db.execute('''SELECT e.message_id FROM fact_evidence e JOIN event_facts f ON f.id=e.fact_id
                      WHERE f.event_id=? AND f.id=?''',(memory_id,data['fact_id']))}
            sources=[r for r in sources if r['id'] in allowed]
            if not sources:raise ValueError('fact_not_in_memory')
        sources.sort(key=lambda r: (r['session_id'], r['seq']))
        fingerprint = _digest([memory_id, sources])
        index, char_offset = _cursor(data, fingerprint)
        if index > len(sources) or (index == len(sources) and char_offset) or (index < len(sources) and char_offset >= len(sources[index]['text'])):
            raise ValueError('invalid_cursor')
        records = []
        while index < len(sources) and budget and len(records) < 20:
            source = sources[index]
            fragment = source['text'][char_offset:char_offset + budget]
            records.append({**_source_ref(source), 'text': fragment, 'text_offset': char_offset,
                            'text_length': len(source['text']), 'fragment_complete': char_offset + len(fragment) == len(source['text'])})
            budget -= len(fragment)
            char_offset += len(fragment)
            if char_offset == len(source['text']):
                index += 1
                char_offset = 0
        return {'ok': True, 'memory_id': memory_id, 'records': records, 'source_count': len(sources),
                'next_cursor': _token(fingerprint, [index, char_offset]) if index < len(sources) else None,
                'read_only': True, 'chat_model_used': False, 'degraded': False}
