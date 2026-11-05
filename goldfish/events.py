"""E: evidence per fact, versioned events and retractable reviewed relations.
All semantic decisions arrive from DSH's configured model; this module is deterministic.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from .engine import now_iso, tokens, similarity


def key(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def identity_matches(identity, text):
    if identity in text:return True
    named=re.findall(r'[\u4e00-\u9fff]{2}-[A-Za-z]+\d{2,10}',identity)
    if named:return any(value in text for value in named)
    return any(code in text for code in re.findall(r'[A-Za-z]{1,8}-?\d{2,10}',identity))


def enabled(db):
    return db.db.execute('PRAGMA user_version').fetchone()[0] >= 6


def migrate(db):
    if enabled(db):
        return
    if db.db.execute('PRAGMA user_version').fetchone()[0] != 5:
        raise ValueError('E_requires_schema5')
    if db.path != ':memory:':
        with closing(sqlite3.connect(db.path+'.before-E-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))) as dest:
            db.db.backup(dest)
    statements = [
        '''CREATE TABLE memory_events(id TEXT PRIMARY KEY REFERENCES summaries(id), identity_key TEXT NOT NULL,
           title TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE TABLE event_facts(id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES memory_events(id),
           text TEXT NOT NULL,aspect TEXT NOT NULL,event_time TEXT,operation TEXT NOT NULL,
           previous_fact TEXT REFERENCES event_facts(id),state TEXT NOT NULL DEFAULT 'current',
           job_id TEXT NOT NULL REFERENCES compact_jobs(id),created_at TEXT NOT NULL)''',
        '''CREATE TABLE fact_evidence(fact_id TEXT NOT NULL REFERENCES event_facts(id),
           message_id TEXT NOT NULL REFERENCES raw_messages(id),quote TEXT NOT NULL,
           start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,PRIMARY KEY(fact_id,message_id,start_offset,end_offset))''',
        '''CREATE TABLE event_decisions(id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES compact_jobs(id),
           event_id TEXT NOT NULL REFERENCES memory_events(id),fact_id TEXT REFERENCES event_facts(id),
           operation TEXT NOT NULL,review_json TEXT NOT NULL,created_at TEXT NOT NULL)''',
        '''CREATE TABLE event_relations(id TEXT PRIMARY KEY,left_id TEXT NOT NULL REFERENCES summaries(id),
           right_id TEXT NOT NULL REFERENCES summaries(id),kind TEXT NOT NULL,state TEXT NOT NULL,
           reason TEXT NOT NULL,evidence_json TEXT NOT NULL,review_json TEXT NOT NULL,
           endpoint_hash TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES compact_jobs(id),
           created_at TEXT NOT NULL,updated_at TEXT NOT NULL)''',
        '''CREATE TABLE relation_history(id INTEGER PRIMARY KEY,relation_id TEXT NOT NULL REFERENCES event_relations(id),
           state TEXT NOT NULL,reason TEXT NOT NULL,evidence_json TEXT NOT NULL,review_json TEXT NOT NULL,
           job_id TEXT NOT NULL REFERENCES compact_jobs(id),created_at TEXT NOT NULL)''',
        'CREATE INDEX facts_event ON event_facts(event_id)',
        'CREATE INDEX facts_evidence_message ON fact_evidence(message_id)',
        'CREATE INDEX relation_from ON event_relations(left_id,state)',
        'CREATE INDEX relation_to ON event_relations(right_id,state)',
        'CREATE INDEX event_identity ON memory_events(identity_key)',
        '''CREATE TABLE memory_terms(term TEXT NOT NULL,memory_id TEXT NOT NULL REFERENCES summaries(id),
           source_id TEXT NOT NULL,PRIMARY KEY(term,memory_id,source_id))''',
        'CREATE INDEX terms_memory ON memory_terms(memory_id)',
    ]
    with db.transaction():
        for statement in statements:
            db.db.execute(statement)
        for (mid,) in db.db.execute('SELECT DISTINCT memory_id FROM compact_claims').fetchall():
            if not mid.startswith('profile:'): refresh_index(db,mid)
        db.db.execute('PRAGMA user_version=6')


def refresh_index(db, ident):
    db.db.execute('DELETE FROM memory_terms WHERE memory_id=?',(ident,))
    summary=db.db.execute('SELECT text FROM summaries WHERE id=?',(ident,)).fetchone()
    if not summary:return
    docs=[('',summary['text'])]+[(m['id'],m['text']) for m in db.evidence_for(ident) if m['role']=='user' and m['source']=='human']
    for sid,text in docs:
        db.db.executemany('INSERT OR IGNORE INTO memory_terms VALUES(?,?,?)',[(term,ident,sid) for term in tokens(text)])


def context(db, parts, candidates):
    """Bound old evidence separately; only u aliases grant new-fact authority."""
    sources = {}
    messages = []
    for index, p in enumerate(parts):
        alias = 'u'+str(index)
        sources[alias] = {'source_id': p['source_id'], 'source': p['source'], 'text': p['available_text'], 'new': True, 'offset':p['start']}
        messages.append({'id': alias,'text': p['available_text'],'role': 'user','event_time': p['source']['event_time']})
    query = ' '.join(p['available_text'] for p in parts)
    # Include exact identities even when the summary omitted a name, plus recent
    # same-session events for explicit anaphora; lexical similarity alone never merges.
    ranked = {r['id']: dict(r) for r in candidates}
    for row in db.db.execute('SELECT e.*,s.text,s.start_time,s.end_time FROM memory_events e JOIN summaries s ON s.id=e.id ORDER BY e.updated_at DESC LIMIT 40'):
        if identity_matches(row['identity_key'],query) or row['id'] in ranked:
            ranked[row['id']] = dict(row)
    rows = sorted(ranked.values(), key=lambda r: (identity_matches(r.get('identity_key','\0'),query), similarity(query,r['text'])), reverse=True)[:12]
    memories, fact_map, memory_map = [], {}, {}
    history_by_id = {}
    for index, row in enumerate(rows):
        alias = 'm'+str(index)
        memory_map[alias] = row['id']
        event = db.db.execute('SELECT * FROM memory_events WHERE id=?',(row['id'],)).fetchone()
        facts = []
        if event:
            for fact in db.db.execute('SELECT * FROM event_facts WHERE event_id=? ORDER BY created_at,id LIMIT 24',(row['id'],)):
                fid = 'f'+str(len(fact_map))
                fact_map[fid] = dict(fact)
                citations = []
                for ev in db.db.execute('SELECT * FROM fact_evidence WHERE fact_id=? ORDER BY message_id LIMIT 3',(fact['id'],)):
                    original = db.get_message(ev['message_id'])
                    if ev['message_id'] not in history_by_id:
                        hid = 'h'+str(len(history_by_id)); history_by_id[ev['message_id']] = hid
                        sources[hid] = {'source_id': original['id'], 'source': original, 'text': original['text'], 'new': False}
                    hid = history_by_id[ev['message_id']]
                    sources[hid].setdefault('visible_quotes',[]).append(ev['quote'][:1200])
                    citations.append({'source':hid,'quote':ev['quote'][:1200]})
                facts.append({'id':fid,'text':fact['text'],'aspect':fact['aspect'],'time':fact['event_time'],
                              'state':fact['state'],'evidence':citations})
        legacy_evidence=[]
        if not event:
            for original in db.evidence_for(row['id'])[:2]:
                if original['role']!='user' or original['source']!='human': continue
                if original['id'] not in history_by_id:
                    hid='h'+str(len(history_by_id)); history_by_id[original['id']]=hid
                    sources[hid]={'source_id':original['id'],'source':original,'text':original['text'],'new':False}
                legacy_evidence.append({'source':history_by_id[original['id']],'quote':original['text'][:1000]})
        memories.append({'id':alias,'text':row['text'][:2200], 'event_key':event['identity_key'] if event else None,
                         'revision':event['revision'] if event else row.get('version',1),'facts':facts,
                         'start_time':row.get('start_time'),'end_time':row.get('end_time'),'legacy_evidence':legacy_evidence})
    relation_map, relations = {}, []
    reverse = {v:k for k,v in memory_map.items()}
    for rel in db.db.execute("SELECT * FROM event_relations WHERE state IN ('supported','candidate') ORDER BY updated_at DESC LIMIT 40"):
        if rel['left_id'] not in reverse and rel['right_id'] not in reverse: continue
        alias = 'r'+str(len(relation_map)); relation_map[alias] = dict(rel)
        relations.append({'id':alias,'from':reverse.get(rel['left_id'],rel['left_id']),
                          'to':reverse.get(rel['right_id'],rel['right_id']),'kind':rel['kind'],'state':rel['state'],'reason':rel['reason']})
        if len(relations)>=8:break
    public = {'messages':messages,'existing':memories,'relations':relations,
              'historical_evidence':[{'id':alias,'role':'user','event_time':item['source']['event_time'],
                                     'text':item['text'][:4000]} for alias,item in sources.items() if not item['new']]}
    # The model may quote only what was actually provided, not invisible tails.
    for alias, item in sources.items():
        if not item['new']: item['text'] = item['text'][:4000]
    # A bounded evidence window is explicit; never send an unbounded event graph
    # into the main model or leave invisible aliases authorized at commit.
    while len(json.dumps(public,ensure_ascii=False))>60000 and len(public['existing'])>1:
        removed=public['existing'].pop()
        memory_map.pop(removed['id'],None)
        allowed_facts={f['id'] for m in public['existing'] for f in m['facts']}
        fact_map={k:v for k,v in fact_map.items() if k in allowed_facts}
        used={c['source'] for m in public['existing'] for f in m['facts'] for c in f['evidence']}
        used.update(c['source'] for m in public['existing'] for c in m['legacy_evidence'])
        public['historical_evidence']=[h for h in public['historical_evidence'] if h['id'] in used]
        sources={k:v for k,v in sources.items() if v['new'] or k in used}
        public['relations']=[r for r in public['relations'] if r['from'] in memory_map or r['to'] in memory_map]
        allowed_rel={r['id'] for r in public['relations']};relation_map={k:v for k,v in relation_map.items() if k in allowed_rel}
        public['candidate_window_truncated']=True
    if len(json.dumps(public,ensure_ascii=False))>60000:
        raise ValueError('event_context_budget_exceeded')
    return public, {'sources':sources,'facts':fact_map,'memories':memory_map,'relations':relation_map}


def _string(value, limit, name):
    if not isinstance(value,str) or not value.strip() or len(value)>limit:
        raise ValueError('invalid_'+name)
    return value.strip()


def citations(items, env, *, require_new=True):
    if not isinstance(items,list) or not 1 <= len(items) <= 8:
        raise ValueError('fact_evidence_required')
    found = []
    for item in items:
        src = env['sources'].get(item.get('source'))
        quote = _string(item.get('quote'),2000,'quote')
        if not src or (quote not in src['text'] and not any(quote in visible for visible in src.get('visible_quotes',[]))):
            raise ValueError('quote_not_in_visible_user_source')
        # An explicit disclaimer that an account is NOT a model guess does not
        # turn the preceding human assertion into a guess. Preserve the quote.
        assertion_check=re.sub(r'(?:不是|并非)模型推测','',quote)
        if re.search(r'(?:不要|禁止|别).{0,12}(?:记住|记忆|保存)|模型推测|预测|假设|[?？]',assertion_check):
            raise ValueError('not_user_asserted_fact')
        offset = src['source']['text'].find(quote,src.get('offset',0))
        if offset < 0: raise ValueError('original_quote_missing')
        found.append({'message_id':src['source_id'],'quote':quote,'start':offset,'end':offset+len(quote),
                      'new':src['new'],'source':src['source'],'alias':item['source']})
    if require_new and not any(e['new'] for e in found):
        raise ValueError('new_human_evidence_required')
    return found


def _review(review, category, ref):
    values = review.get(category)
    if not isinstance(values,list): raise ValueError('missing_semantic_review')
    matches = [v for v in values if v.get('ref')==ref]
    if len(matches)!=1 or matches[0].get('verdict')!='supported':
        raise ValueError('semantic_review_not_supported_'+category)
    _string(matches[0].get('reason'),1000,'review_reason')
    return matches[0]


def _save_sources(db, evidence):
    for ev in evidence:
        if not ev['new']: continue
        src = ev['source']
        db.add_message(src['namespace']+':'+src['session_id'],'user',src['text'],src['event_time'],ev['message_id'],'human')


def endpoint_hash(db, left, right, evidence=None):
    # Bind the cited facts, so an unrelated event supplement does not invalidate
    # every edge. Correction of a cited fact still invalidates that review.
    sources={e['message_id'] for e in evidence or []}
    values=[]
    for ident in (left,right):
        event=db.db.execute('SELECT identity_key FROM memory_events WHERE id=?',(ident,)).fetchone()
        if event:
            facts=[dict(r) for r in db.db.execute('''SELECT DISTINCT f.id,f.text,f.state FROM event_facts f
                    JOIN fact_evidence e ON e.fact_id=f.id WHERE f.event_id=? ORDER BY f.id''',(ident,))
                   if any(r[0] in sources for r in db.db.execute('SELECT message_id FROM fact_evidence WHERE fact_id=?',(r['id'],)))]
            values.append([ident,event['identity_key'],facts])
        else:
            values.append(dict(db.db.execute('SELECT id,text,version,status FROM summaries WHERE id=?',(ident,)).fetchone()))
    return key(values)


def apply(db, job_id, result, env):
    if not isinstance(result,dict) or set(result)!={'plan','review'}:
        raise ValueError('E_plan_and_main_model_review_required')
    plan, review = result['plan'], result['review']
    if not isinstance(plan,dict) or set(plan)!={'events','relations','revoke'}:
        raise ValueError('invalid_event_plan')
    if any(not isinstance(plan[k],list) or len(plan[k])>n for k,n in [('events',8),('relations',12),('revoke',8)]):
        raise ValueError('event_plan_too_large')
    saved, duplicates, event_refs, fact_refs, proposed_facts = [], 0, {}, set(), {}
    for group in plan['events']:
        ref = _string(group.get('ref'),40,'event_ref')
        if ref in event_refs: raise ValueError('duplicate_event_ref')
        title = _string(group.get('title'),180,'event_title')
        identity = _string(group.get('event_key'),180,'event_identity')
        approved = _review(review,'events',ref)
        anchors = citations(group.get('anchors'),env)
        existing = group.get('existing')
        ident = env['memories'].get(existing) if existing else None
        if existing and not ident: raise ValueError('unobserved_existing_event')
        old = db.db.execute('SELECT * FROM memory_events WHERE id=?',(ident,)).fetchone() if ident else None
        if ident and not old: raise ValueError('legacy_memory_not_an_event_merge_target')
        # Joining needs a stable named identity or explicit anaphora with a
        # separately approved same-event decision. Similarity is never sufficient.
        anchor_text=' '.join(e['quote'] for e in anchors if e['new'])
        if old:
            if identity != old['identity_key']: raise ValueError('event_identity_changed')
            if not identity_matches(identity,anchor_text) and not re.search(r'这次|那次|同一次|刚才|上述|前述|此前那|该次',anchor_text):
                raise ValueError('specific_identity_or_explicit_reference_required')
        elif identity not in anchor_text:
            raise ValueError('event_key_must_be_literal_user_anchor')
        if not ident and db.db.execute('SELECT 1 FROM memory_events WHERE identity_key=?',(identity,)).fetchone():
            raise ValueError('existing_event_identity_requires_merge_decision')
        facts = group.get('facts')
        if not isinstance(facts,list) or not 1 <= len(facts) <= 20: raise ValueError('invalid_event_facts')
        if not ident:
            ident='event:'+key([job_id,ref])
            _save_sources(db,anchors)
            db.add_summary(title,[e['message_id'] for e in anchors],summary_id=ident)
            stamp=now_iso()
            db.db.execute('INSERT INTO memory_events VALUES(?,?,?,?,?,?)',(ident,identity,title,1,stamp,stamp))
        event_refs[ref]=ident
        for fact in facts:
            fref=_string(fact.get('ref'),40,'fact_ref')
            if fref in fact_refs: raise ValueError('duplicate_fact_ref')
            fact_refs.add(fref)
            verdict=_review(review,'facts',fref)
            text=_string(fact.get('text'),1000,'fact_text')
            aspect=fact.get('aspect'); operation=verdict.get('recommended_operation')
            if type(verdict.get('adds_information')) is not bool:
                raise ValueError('semantic_novelty_classification_required')
            target=verdict.get('target_fact',fact.get('target_fact'))
            if verdict['adds_information'] is False and operation!='duplicate':
                raise ValueError('equivalent_fact_target_required')
            if aspect not in {'context','cause','process','outcome','decision','feeling','meaning'}:
                raise ValueError('invalid_fact_aspect')
            if operation not in {'new','duplicate','supplement','correction','change','meaning_change'}:
                raise ValueError('invalid_fact_operation')
            ev=citations(fact.get('evidence'),env)
            previous=env['facts'].get(target)
            if operation in {'duplicate','correction','change','meaning_change'} and not previous:
                raise ValueError('prior_fact_required')
            if previous and previous['event_id']!=ident: raise ValueError('cross_event_fact_merge')
            if operation=='correction' and not re.search(r'纠正|更正|说错|不是.{0,30}而是|记错|实际是|应该是',' '.join(e['quote'] for e in ev if e['new'])):
                raise ValueError('explicit_correction_required')
            if aspect in {'feeling','meaning'} and verdict.get('user_expression') is not True:
                raise ValueError('inferred_feeling_not_user_expression')
            when=fact.get('time')
            if when is not None:
                if not isinstance(when,str) or not re.fullmatch(r'\d{4}(?:-\d{2})?(?:-\d{2})?',when):
                    raise ValueError('invalid_fact_time_precision')
                datetime.fromisoformat(when+'-01-01' if len(when)==4 else when+'-01' if len(when)==7 else when)
            if operation in {'new','supplement'} and aspect not in {'feeling','meaning'}:
                normalized=lambda v: ''.join(v.casefold().split())
                exact=next((dict(r) for r in db.db.execute('SELECT * FROM event_facts WHERE event_id=? AND aspect=? AND state=?',(ident,aspect,'current'))
                            if normalized(r['text'])==normalized(text) and r['event_time']==when),None)
                if exact:previous=exact;operation='duplicate'
            _save_sources(db,ev)
            if operation=='duplicate':
                fid=previous['id']; duplicates+=1
            else:
                fid='fact:'+key([job_id,ref,fref])
                db.db.execute('INSERT INTO event_facts VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (fid,ident,text,aspect,when,operation,previous['id'] if previous else None,'current',job_id,now_iso()))
                if previous and operation in {'correction','change','meaning_change'}:
                    db.db.execute('UPDATE event_facts SET state=? WHERE id=?',('corrected' if operation=='correction' else 'historical',previous['id']))
            for evidence in ev:
                db.db.execute('INSERT OR IGNORE INTO fact_evidence VALUES(?,?,?,?,?)',
                    (fid,evidence['message_id'],evidence['quote'],evidence['start'],evidence['end']))
                if evidence['new']:
                    claim=key([evidence['message_id'],evidence['quote'],'events'])
                    db.db.execute('INSERT OR IGNORE INTO compact_claims VALUES(?,?,?,?,?,?,?)',
                        (claim,evidence['message_id'],evidence['quote'],ident,job_id,'event_fact',now_iso()))
            proposed_facts[fref]={'id':fid,'event_id':ident}
            db.db.execute('INSERT INTO event_decisions VALUES(?,?,?,?,?,?,?)',
                (key([job_id,ref,fref]),job_id,ident,fid,operation,json.dumps(verdict,ensure_ascii=False),now_iso()))
        # Aggregate is assembled only from individually reviewed facts. No new
        # model narrative can silently add facts supported merely by the union.
        fact_rows=db.db.execute('SELECT * FROM event_facts WHERE event_id=? ORDER BY created_at,id',(ident,)).fetchall()
        body=title+'\n'+'\n'.join('['+r['aspect']+';'+(r['event_time'] or '时间未明')+';'+r['state']+'] '+r['text'] for r in fact_rows)
        if len(body)>12000: raise ValueError('event_view_budget_exceeded')
        ids=[r[0] for r in db.db.execute('SELECT DISTINCT e.message_id FROM fact_evidence e JOIN event_facts f ON f.id=e.fact_id WHERE f.event_id=?',(ident,))]
        times=[r['event_time'] for r in fact_rows if r['event_time']]
        db.update_summary(ident,body,ids,min(times) if times else None,max(times) if times else None)
        db.db.execute('UPDATE memory_events SET revision=revision+1,updated_at=? WHERE id=?',(now_iso(),ident))
        theme=group.get('theme','')
        if theme:
            db.attach_theme(db.add_theme(_string(theme,80,'theme')),ident,'来源明确的事件主题；不证明关系')
        saved.append(ident)
        refresh_index(db,ident)
    endpoints={**env['memories'],**event_refs}
    relation_proposals=list(plan['relations'])
    if any(r.get('ref') in env['relations'] for r in relation_proposals):
        raise ValueError('relation_ref_collides_with_visible_existing')
    known_refs={r.get('ref') for r in relation_proposals}
    reverse={v:k for k,v in env['memories'].items()}
    for reviewed in review.get('relations',[]):
        old=env['relations'].get(reviewed.get('ref'))
        if old and reviewed.get('ref') not in known_refs and old['left_id'] in reverse and old['right_id'] in reverse:
            relation_proposals.append({'ref':reviewed['ref'],'from':reverse[old['left_id']],'to':reverse[old['right_id']],
              'kind':old['kind'],'reason':old['reason'],'evidence':reviewed.get('endpoint_evidence',[])+reviewed.get('relationship_evidence',[])})
    for proposed in relation_proposals:
        ref=_string(proposed.get('ref'),40,'relation_ref')
        left,right=endpoints.get(proposed.get('from')),endpoints.get(proposed.get('to'))
        if not left or not right or left==right: raise ValueError('invalid_relation_endpoints')
        kind=proposed.get('kind')
        if kind not in {'same_event','same_entity','temporal_sequence','similar_context','contradiction','comparison','same_topic','emotion_echo','possible_cause','reported_influence'}:
            raise ValueError('unconfirmed_causality_not_allowed')
        reviews=[r for r in review.get('relations',[]) if r.get('ref')==ref]
        if len(reviews)!=1: raise ValueError('relation_review_required')
        verdict=reviews[0]
        _string(verdict.get('reason'),1000,'relation_review_reason')
        selected=proposed.get('evidence',[])+verdict.get('endpoint_evidence',[])+verdict.get('relationship_evidence',[])
        for field,target in (('from_fact',left),('to_fact',right)):
            chosen=env['facts'].get(verdict.get(field)) or proposed_facts.get(verdict.get(field))
            if not chosen:continue
            if chosen['event_id']!=target:raise ValueError('relation_fact_wrong_endpoint')
            for ev in db.db.execute('SELECT * FROM fact_evidence WHERE fact_id=? LIMIT 2',(chosen['id'],)):
                alias=next((k for k,v in env['sources'].items() if v['source_id']==ev['message_id'] and
                            (ev['quote'][:1200] in v['text'] or ev['quote'][:1200] in v.get('visible_quotes',[]))),None)
                if alias:selected.append({'source':alias,'quote':ev['quote'][:1200]})
        selected=list({(e.get('source'),e.get('quote')):e for e in selected}.values())
        evidence=citations(selected,env,require_new=False)
        _save_sources(db,evidence)
        # A relation needs source coverage of both endpoints, plus a review of
        # the RELATION, not two unrelated true statements.
        # Sharing a raw message is not enough: for structured events the quote
        # must contain, or be contained by, an actual fact's evidence range.
        def covers_endpoint(ident):
            fact_ranges=db.db.execute('''SELECT e.* FROM fact_evidence e JOIN event_facts f
              ON f.id=e.fact_id WHERE f.event_id=?''',(ident,)).fetchall()
            if fact_ranges:
                return any(e['message_id']==f['message_id'] and
                  (f['start_offset']<=e['start']<e['end']<=f['end_offset'] or
                   e['start']<=f['start_offset']<f['end_offset']<=e['end'])
                  for e in evidence for f in fact_ranges)
            return any(e['message_id']==r['id'] for e in evidence for r in db.evidence_for(ident))
        coverage=all(covers_endpoint(ident) for ident in (left,right))
        supported=verdict.get('verdict')=='supported' and verdict.get('relationship_supported') is True and coverage
        verdict={**verdict,'endpoint_evidence_coverage':coverage}
        if kind in {'reported_influence','emotion_echo'}:
            supported = supported and any(re.search(r'因为|促使|让我|使我|影响|因此|源于|原因|同样|感受|意义|觉得|because',e['quote'])
                         and not re.search(r'无关|没有.{0,12}影响|不相关',e['quote']) for e in evidence)
        if kind=='possible_cause': supported=False
        reason=_string(proposed.get('reason'),1000,'relation_reason')
        state='supported' if supported else 'rejected' if verdict.get('verdict')=='rejected' else 'candidate'
        if kind not in {'temporal_sequence','reported_influence','possible_cause'}: left,right=sorted((left,right))
        rid='relation:'+key([left,right,kind])
        serial=[{k:e[k] for k in ('message_id','quote','start','end')} for e in evidence]
        existing=db.db.execute('SELECT * FROM event_relations WHERE id=?',(rid,)).fetchone()
        if existing and existing['state']=='revoked': state='revoked'
        stamp=now_iso()
        if existing and existing['state']=='supported' and state=='candidate' and existing['endpoint_hash']==endpoint_hash(db,left,right,json.loads(existing['evidence_json'])):
            db.db.execute('INSERT INTO relation_history(relation_id,state,reason,evidence_json,review_json,job_id,created_at) VALUES(?,?,?,?,?,?,?)',
              (rid,state,reason,json.dumps(serial,ensure_ascii=False),json.dumps(verdict,ensure_ascii=False),job_id,stamp))
            continue
        db.db.execute('''INSERT INTO event_relations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET state=excluded.state,reason=excluded.reason,evidence_json=excluded.evidence_json,
          review_json=excluded.review_json,endpoint_hash=excluded.endpoint_hash,updated_at=excluded.updated_at''',
          (rid,left,right,kind,state,reason,json.dumps(serial,ensure_ascii=False),json.dumps(verdict,ensure_ascii=False),endpoint_hash(db,left,right,serial),job_id,stamp,stamp))
        db.db.execute('INSERT INTO relation_history(relation_id,state,reason,evidence_json,review_json,job_id,created_at) VALUES(?,?,?,?,?,?,?)',
          (rid,state,reason,json.dumps(serial,ensure_ascii=False),json.dumps(verdict,ensure_ascii=False),job_id,stamp))
    for proposed in plan['revoke']:
        old=env['relations'].get(proposed.get('relation'))
        if not old: raise ValueError('unknown_relation_to_revoke')
        verdict=_review(review,'revoke',proposed.get('ref'))
        evidence=citations(proposed.get('evidence'),env)
        if not any(re.search(r'无关|没有.{0,12}关系|不相关|没有影响|不是.{0,20}原因|撤销|否认',e['quote']) for e in evidence if e['new']):
            raise ValueError('explicit_relationship_denial_required')
        _save_sources(db,evidence)
        serial=[{k:e[k] for k in ('message_id','quote','start','end')} for e in evidence]
        reason=_string(proposed.get('reason'),1000,'revoke_reason')
        db.db.execute("UPDATE event_relations SET state='revoked',updated_at=? WHERE id=?",(now_iso(),old['id']))
        db.db.execute('INSERT INTO relation_history(relation_id,state,reason,evidence_json,review_json,job_id,created_at) VALUES(?,?,?,?,?,?,?)',
          (old['id'],'revoked',reason,json.dumps(serial,ensure_ascii=False),json.dumps(verdict,ensure_ascii=False),job_id,now_iso()))
    return saved, duplicates
