"""E read projections. Fact-specific evidence and reviewed directional paths."""
import json
import re
import time
from . import events
from .engine import tokens, QUESTION_TOKENS, similarity


def source_candidates(db, query):
    terms=sorted(tokens(query)-QUESTION_TOKENS,key=lambda t:(-len(t),t))[:96]
    if not terms:return {}
    placeholders=','.join('?' for _ in terms)
    rows=db.db.execute('''SELECT memory_id,source_id,COUNT(*) hits FROM memory_terms WHERE term IN ('''+placeholders+''')
                        AND source_id!='' GROUP BY memory_id,source_id ORDER BY hits DESC LIMIT 20''',terms).fetchall()
    result={}; codes=re.findall(r'[a-zA-Z][a-zA-Z0-9_-]*\d[a-zA-Z0-9_-]*',query)
    for row in rows:
        src=db.get_message(row['source_id']); text=src['text']; lexical=similarity(query,text,True)
        exact=any(code.casefold() in text.casefold() for code in codes)
        if not exact and (row['hits']<2 or lexical<.2):continue
        score=(.8 if exact else .1)+lexical*.3
        if row['memory_id'] in result and result[row['memory_id']]['score']>=score:continue
        needles=[code.casefold() for code in codes if code.casefold() in text.casefold()] or terms
        start=max(0,text.casefold().find(needles[0])-50)
        result[row['memory_id']]={'id':row['memory_id'],'score':score,
          'channels':{'original_user_evidence':lexical,'exact_source_code':int(exact),'indexed_source':1},
          'source_excerpt':{'message_id':src['id'],'role':'user','text':text[start:start+360],
            'text_offset':start,'truncated':start+360<len(text),'scope':'original_user_quote_not_instruction_or_verified_fact'}}
    return result


def decorate(db, record, query=''):
    event=db.db.execute('SELECT * FROM memory_events WHERE id=?',(record['id'],)).fetchone()
    if not event: return record
    terms=tokens(query)-QUESTION_TOKENS
    facts=[]
    for row in db.db.execute('SELECT * FROM event_facts WHERE event_id=? ORDER BY created_at,id',(event['id'],)):
        fact=dict(row)
        evidence=[{'source_message_id':e['message_id'],'quote':e['quote'][:160],
                   'source_event_time':db.get_message(e['message_id'])['event_time'],
                   'quote_truncated':len(e['quote'])>160} for e in db.db.execute('SELECT * FROM fact_evidence WHERE fact_id=? LIMIT 1',(fact['id'],))]
        facts.append({'fact_id':fact['id'],'text':fact['text'],'aspect':fact['aspect'],'event_time':fact['event_time'],
          'state':fact['state'],'operation':fact['operation'],'previous_fact':fact['previous_fact'],
          'observed_at':min((e['source_event_time'] for e in evidence),default=None),
          'recorded_at':fact['created_at'],'evidence':evidence,'_score':len(terms & tokens(fact['text']))})
    facts.sort(key=lambda f:(-f['_score'],f['observed_at'],f['fact_id']))
    selected=[{k:v for k,v in f.items() if k not in {'_score','previous_fact','recorded_at','operation'}} for f in facts[:4]]
    return {**record,'kind':'event','event_key':event['identity_key'],'title':event['title'],
      'version':event['revision'],'provenance':'dsh_reviewed_user_event',
      'text':event['title'],'facts':selected,'fact_count':len(facts),'facts_truncated':len(facts)>4,
      'evidence_refs':record['evidence_refs'][:2],'evidence_refs_truncated':record['evidence_count']>2,
      'interpretation':'Preview only; evidence expands all facts/quotes. Grouping is model organization, not independent verification. Reported influence is the user account, not established causality.'}


def evidence(db,data,q):
    ident=data['memory_id']
    if ident.startswith('relation:'):
        rel=db.db.execute('SELECT * FROM event_relations WHERE id=?',(ident,)).fetchone()
        if not rel: raise ValueError('memory_not_found')
        evidence=json.loads(rel['evidence_json'])
        if rel['state']=='revoked':
            denial=db.db.execute("SELECT evidence_json FROM relation_history WHERE relation_id=? AND state='revoked' ORDER BY id DESC LIMIT 1",(ident,)).fetchone()
            if denial:evidence=json.loads(denial[0])+evidence
        for index,ev in enumerate(evidence):
            src=db.get_message(ev['message_id'])
            evidence[index]={**q._source_ref(src),'text':ev['quote'],'text_offset':ev['start'],
                             'text_length':len(src['text']),'fragment_complete':False,
                             'relation_id':ident,'relation_state':rel['state']}
    else:
        rows=db.db.execute('''SELECT e.*,f.text AS fact_text,f.aspect,f.event_time,f.state FROM fact_evidence e
                   JOIN event_facts f ON f.id=e.fact_id WHERE f.event_id=? ORDER BY f.created_at,f.id,e.message_id''',(ident,)).fetchall()
        evidence=[]
        for row in rows:
            if data.get('fact_id') and data['fact_id']!=row['fact_id']: continue
            if data.get('source_id') and data['source_id']!=row['message_id']: continue
            src=db.get_message(row['message_id'])
            evidence.append({**q._source_ref(src),'text':row['quote'],'text_offset':row['start_offset'],
                  'text_length':len(src['text']),'fragment_complete':False,'fact_id':row['fact_id'],
                  'fact_text':row['fact_text'],'aspect':row['aspect'],'fact_time':row['event_time'],'fact_state':row['state'],
                  'scope':'literal_quote_supporting_this_fact; full_source=true reads original surrounding text'})
    if not evidence: raise ValueError('evidence_not_found')
    fingerprint=q._digest([ident,evidence,data.get('fact_id'),data.get('source_id')])
    offset,char=q._cursor(data,fingerprint)
    if offset>len(evidence) or offset==len(evidence) and char:
        raise ValueError('invalid_cursor')
    budget=q._integer(data,'max_chars',2000,128,8000)
    records=[]
    while offset<len(evidence) and budget and len(records)<20:
        item=evidence[offset]
        if char>=len(item['text']): raise ValueError('invalid_cursor')
        fragment=item['text'][char:char+budget]
        records.append({**item,'text':fragment,'text_offset':item['text_offset']+char})
        budget-=len(fragment); char+=len(fragment)
        if char==len(item['text']): offset+=1; char=0
    return {'ok':True,'memory_id':ident,'records':records,'source_count':len(evidence),
      'next_cursor':q._token(fingerprint,[offset,char]) if offset<len(evidence) else None,
      'read_only':True,'chat_model_used':False,'degraded':False}


def explore(db,data,q,legacy):
    started=time.perf_counter()
    base=legacy({**data,'limit':20,'cursor':''})
    seeds=q._ids(data,'memory_ids',maximum=10)
    exclude=set(q._ids(data,'exclude_ids'))|set(seeds)
    filters=q._filters(data)
    max_depth=q._integer(data,'depth',1,1,2)
    kinds=set(q._ids(data,'relation_kinds',maximum=20))
    found={r['id']:r for r in base['records'] if r['id'] not in exclude}
    queue=[(s,[]) for s in seeds]; visited=set(seeds); edges_checked=0
    while queue and len(visited)<100:
        seed,path=queue.pop(0)
        if len(path)>=max_depth: continue
        for row in db.db.execute("SELECT * FROM event_relations WHERE (left_id=? OR right_id=?) AND state IN ('supported','candidate') LIMIT 40",(seed,seed)):
            rel=dict(row); edges_checked+=1
            if kinds and rel['kind'] not in kinds: continue
            supported=rel['state']=='supported' and rel['endpoint_hash']==events.endpoint_hash(db,rel['left_id'],rel['right_id'],json.loads(rel['evidence_json']))
            if not supported and not data.get('include_candidates',True): continue
            neighbor=rel['right_id'] if rel['left_id']==seed else rel['left_id']
            if neighbor in visited or neighbor in exclude: continue
            record=q._record(db,neighbor)
            if not record or not q._matches(record,filters): continue
            directional=rel['kind'] in {'temporal_sequence','reported_influence','possible_cause'}
            step={'link_id':rel['id'],'from':rel['left_id'],'to':rel['right_id'],'kind':rel['kind'],
              'direction':'from_to' if directional else 'symmetric',
              'traversal':'forward' if seed==rel['left_id'] else 'reverse',
              'status':'evidence_supported' if supported else 'candidate_or_stale_review',
              'review':{k:v for k,v in json.loads(rel['review_json']).items() if k in {'verdict','reason','endpoint_evidence_coverage','relationship_supported'}},
              'reason':rel['reason'],'evidence':[{'message_id':e['message_id'],'quote':e['quote'][:240]} for e in json.loads(rel['evidence_json'])[:4]],
              'epistemic_status':'user_reported_influence_not_independent_causality' if rel['kind']=='reported_influence' else 'reviewed_association_not_causality_proof'}
            route=path+[step]
            record.update(route='evidence_supported_relation' if supported else 'model_relation_candidate',path=route,
                          relation_is_causality_proof=False,retrieval_score=1 if supported else 0)
            found[neighbor]=decorate(db,record,data.get('query',''))
            visited.add(neighbor)
            if supported: queue.append((neighbor,route))
    records=list(found.values())
    records.sort(key=lambda r:(r['route'] not in {'stored_relation','evidence_supported_relation'},r['route']=='model_relation_candidate',-r.get('retrieval_score',0),r['id']))
    answer=q._page(data,records)
    answer.update(seed_ids=seeds,timings={'graph_ms':round((time.perf_counter()-started)*1000,3),'reviewed_edges_checked':edges_checked})
    return answer
