"""Small, bounded multi-channel recall. Similarity proposes candidates, not facts."""
from __future__ import annotations

from datetime import date
import json
import math
import re

from .engine import _time_only_query, similarity, tokens, QUESTION_TOKENS, year_filter


def profile_candidates(db, query, auxiliary=None, limit=4):
    auxiliary=auxiliary or {}
    behavior=any(s in query for s in ('自称','行为证据','表现过','自身画像','实际做','数字修正','纠错','合作经验'))
    interaction=any(s in query for s in ('我希望','交互','交流','聊天','提醒','先帮','沟通','反馈','机械','清单'))
    rows=[]
    for source in db.db.execute("SELECT * FROM profile WHERE status='active'"):
        row=dict(source); cid='profile:'+row['id'];lexical=similarity(query,row['text'],True)
        sem=auxiliary.get(cid,0)
        score=lexical+max(0,sem-.45)*.6
        if behavior and row['kind']=='ai_observed':score+=.8
        if interaction and row['kind'] in {'interaction_expectation','feedback'}:score+=.08
        if score<.07:continue
        rows.append({'id':cid,'text':row['text'],'kind':'profile:'+row['kind'],
            'evidence_ids':json.loads(row['evidence_ids']),'score':score,'channels':{'keyword':lexical,'embedding':sem}})
    return sorted(rows,key=lambda r:(-r['score'],r['id']))[:limit]


def candidate_pool(db, query, *, semantic=None, auxiliary=None, mode='hybrid', limit=8, today=None, depth=0, model_identity=None):
    """Weighted rank fusion, with protected exact/temporal anchors and graph cap.

    All channels see the same active summary snapshot. A graph-only candidate
    can fill at most one slot and cannot displace any directly ranked candidate.
    """
    today=today or date.today(); semantic=semantic or {};auxiliary=auxiliary or {}
    with db.lock:
        rows=db._summary_with_context();by_id={r['id']:r for r in rows}
        years=year_filter(query,today)
        explicit_dates=re.findall(r'(20\d{2})[年/-](\d{1,2})[月/-](\d{1,2})',query)
        codes=re.findall(r'\b[A-Za-z]{1,8}-\d{2,10}\b',query)
        query_terms=tokens(query)-QUESTION_TOKENS
        document_terms={r['id']:tokens(r['text'])-QUESTION_TOKENS for r in rows}
        df={term:sum(term in ts for ts in document_terms.values()) for term in query_terms}
        signals={}
        for row in rows:
            sid=row['id']; ts=document_terms[sid]
            keyword=sum(math.log(1+(len(rows)+1)/(df[t]+1)) for t in query_terms & ts)/max(1,math.sqrt(len(ts)))
            topical=similarity(query,row['theme_titles'] or '',True)
            period=any(str(y) in (row['start_time'] or '')+(row['end_time'] or '')+row['text'] for y in years)
            exact=any(code.casefold() in row['text'].casefold() for code in codes)
            if explicit_dates:
                exact=exact or any(f'{int(y)}-{int(m):02}-{int(d):02}' in (row['start_time'] or '') or f'{int(y)}年{int(m)}月{int(d)}日' in row['text'] for y,m,d in explicit_dates)
            signals[sid]={'keyword':keyword,'embedding':semantic.get(sid,0),'theme':topical,
                          'time':float(period),'exact':float(exact)}
        # Optional auxiliary embeddings can nominate members; they do not
        # establish the theme/edge's truth or replace source summary relevance.
        for doc,score in auxiliary.items():
            if score<.5:continue
            if doc.startswith('theme:'):
                ids=[r[0] for r in db.db.execute('SELECT summary_id FROM theme_members WHERE theme_id=?',(doc[6:],))]
            elif doc.startswith('link:'):
                ids=[sid for e in db.active_reviewed_links() if e['id']==doc[5:] for sid in (e['left_id'],e['right_id'])]
            else:continue
            for sid in ids:
                if sid in signals:signals[sid]['theme']=max(signals[sid]['theme'],score*.5)
        channels={'embedding':1.0} if mode=='embedding' else {'keyword':1.0,'embedding':1.0,'theme':.45,'time':.5}
        ranked={}; fused={}
        for channel,weight in channels.items():
            threshold=.45 if channel=='embedding' else 0
            ids=[sid for sid in signals if signals[sid][channel]>threshold]
            if channel=='time' and not _time_only_query(query):
                ids=[sid for sid in ids if signals[sid]['keyword']>0 or signals[sid]['theme']>0 or signals[sid]['embedding']>.5]
            ids.sort(key=lambda sid:(-signals[sid][channel],-signals[sid]['keyword'],sid))
            ranked[channel]=ids[:limit]
            for rank,sid in enumerate(ids[:limit]):fused[sid]=fused.get(sid,0)+weight/(8+rank)
        for sid in fused:
            if signals[sid]['exact']:fused[sid]+=.4
            # Single explicit day and known other event dates cannot be rescued
            # by an attractive vector score. Comparison questions keep periods.
            if len(explicit_dates)==1 and not signals[sid]['exact'] and by_id[sid]['start_time']:
                fused[sid]*=.35
        order=sorted(fused,key=lambda sid:(-fused[sid],sid))
        # Reserve a representative for each requested year when relevant. This
        # is still a candidate, never a claim about change or causation.
        protected=[]
        if len(years)>1:
            for year in sorted(years):
                choices=[sid for sid in order if str(year) in (by_id[sid]['start_time'] or '')+by_id[sid]['text'] and signals[sid]['keyword']>0]
                if choices:protected.append(choices[0])
        exact=[sid for sid in order if signals[sid]['exact']]
        ordered=list(dict.fromkeys(exact+protected+order))[:limit]
        graph=[]
        if depth and len(ordered)<limit:
            anchors=set(ordered[:3]);frontier=set(anchors)
            for _ in range(min(2,depth)):
                nxt=set()
                for edge in db.active_reviewed_links():
                    if edge['left_id'] in frontier or edge['right_id'] in frontier:
                        nxt.update((edge['left_id'],edge['right_id']))
                        graph.append({'endpoints':[edge['left_id'],edge['right_id']],'kind':edge['effective_kind'],'reason':edge['review_reason']})
                frontier=nxt-anchors;anchors|=nxt
            extra=[sid for sid in frontier if sid in by_id and sid not in ordered]
            if extra:ordered.extend(sorted(extra)[:1])
        candidates=[{**by_id[sid],'channels':signals[sid],'score':fused.get(sid,0)} for sid in ordered[:limit]]
        # A question about changed conditions can mention the old decision but
        # omit the words for its supporting conditions. At most two document
        # neighbours of protected/direct anchors enter the bounded ranker pool.
        # This is candidate expansion, never an asserted relationship or cause.
        if mode!='embedding' and model_identity and any(s in query for s in ('条件','变化','为什么','为何')):
            support=[]
            for anchor in ordered[:2]:
                stored=db.db.execute('SELECT vector_json,model_identity,content_hash FROM summary_vectors WHERE summary_id=?',(anchor,)).fetchone()
                if not stored or stored['model_identity']!=model_identity:continue
                import hashlib
                if stored['content_hash']!=hashlib.sha256(by_id[anchor]['text'].encode()).hexdigest():continue
                related=db.semantic_scores(json.loads(stored['vector_json']),model_identity,min_score=.65)
                for sid,score in sorted(related.items(),key=lambda pair:-pair[1]):
                    row=by_id[sid]
                    if sid in ordered or sid in support:continue
                    if years and row['start_time'] and not any(str(y) in row['start_time'] for y in years):continue
                    support.append(sid)
                    if len(support)>=2:break
                if len(support)>=2:break
            candidates.extend({**by_id[sid],'channels':{**signals[sid],'support_candidate':1},'score':0} for sid in support)
        profiles=profile_candidates(db,query,auxiliary,limit=3)
        return {'summaries':candidates,'profiles':profiles,'relations':graph[:5],
            'channels':ranked,'active_count':len(rows),'candidate_count':len(candidates)+len(profiles)}


def direct_plan(pool, query, context='', limit=3):
    """Cheap modes make no semantic-coverage claims from vector similarity."""
    from .engine import _covered
    rows=pool['summaries'][:limit]
    profile_query=any(s in query for s in ('我希望','交互','自称','行为证据','实际做','数字修正','机械','清单','先帮'))
    if profile_query and pool['profiles']:
        rows=pool['profiles'][:1]+rows[:max(0,limit-1)]
    selected=[]
    for row in rows:
        full=_covered(row['text'],context)
        selected.append({'id':row['id'],'text':'' if full else row['text'],'why':'候选召回；相似度不证明事实或因果',
            'state':'full' if full else 'missing' if not context.strip() else 'uncertain',
            'facts':[],'validation':'exact_text_only' if full else 'conservative_no_entailment_claim'})
    return {'intent':'unknown','selected':selected,'measurement':None,'reviewer_kind':'retrieval',
            'candidate_count':pool['candidate_count'],'active_count':pool['active_count']}
