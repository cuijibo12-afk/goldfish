"""Real-model phase C evaluation. Gold and source files are never modified."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from goldfish.engine import MemoryDB
from goldfish.providers import ChatModel,load_config
from goldfish.semantic import RECALL_PROMPT,plan_recall,validate_coverage
from goldfish.relations import review_relations
from goldfish.consolidate import consolidate_session
from .run_real import export_database,dump,latency
from .scoring import score_run

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT/'evals/results/deepseek-flash-2026-11-04/validated'

class MeteredModel(ChatModel):
    def __init__(self,budget=160):
        super().__init__();self.calls=[];self.budget=budget;self.guard=threading.Lock()
    def generate_json(self,inputs,**kwargs):
        with self.guard:
            if len(self.calls)>=self.budget: raise RuntimeError('real_model_budget_exhausted')
            record={'call':len(self.calls),'purpose':kwargs.get('purpose'),'inputs':inputs};self.calls.append(record)
        try:
            output=super().generate_json(inputs,**kwargs);record['output']=output;return output
        except Exception as err:record['error_type']=type(err).__name__;raise
        finally:record['measurement']=self.last_call


def load_old_class(output):
    path=output/'before_engine.py'
    path.write_bytes(subprocess.check_output(['git','show','17bb7ef:goldfish/engine.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('phase_c_before_engine',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.MemoryDB


def populate(db,corpus,memory):
    for session in corpus['sessions']:
        for msg in session['messages']:
            db.add_message(session['session_id'],msg['role'],msg['text'],msg['event_time'],msg['id'],msg.get('source','human'))
    for summary in memory['summaries']:
        db.add_summary(summary['text'],summary['evidence_ids'],summary.get('start_time'),summary.get('end_time'),summary_id=summary['id'])
    themes={r['id']:r for r in memory['themes']};theme_ids={}
    for row in memory['theme_members']:
        old=themes[row['theme_id']];tid=theme_ids.setdefault(old['id'],db.add_theme(old['title'],old['description']))
        db.attach_theme(tid,row['summary_id'],row['reason'])
    for row in memory['profiles']: db.add_profile(row['kind'],row['text'],row['evidence_ids'])
    mapping={}
    for row in memory['links']:
        kind=row['kind']
        if db.__class__.__module__=='phase_c_before_engine':
            kind={'same_entity':'same_person','temporal_sequence':'chronological','similar_context':'contextual','contradiction':'contrast','comparison':'contrast'}.get(kind,kind)
        mapping[row['id']]=db.add_link(row['left_id'],row['right_id'],kind,row['reason'],row['confidence'])
    return mapping


def audit_existing(db,model):
    memory=export_database(db);summaries={r['id']:r for r in memory['summaries']};links=memory['links'];reviews=[]
    for start in range(0,len(links),12):
        group=links[start:start+12];sources={}
        candidates=[]
        for edge in group:
            a,b=summaries[edge['left_id']],summaries[edge['right_id']]
            for sid in (a['id'],b['id']):sources.update({r['id']:r for r in db.evidence_for(sid)})
            candidates.append({'id':edge['id'],'kind':edge['kind'],'reason':edge['reason'],'left':a,'right':b})
        rows,measurement,raw=review_relations(model,candidates,sources,timeout=60)
        for row in rows:db.record_link_review(row['id'],row,model.cfg.model)
        reviews.extend(rows)
    return reviews


def apply_plan(db,model,query,depth=0,timeout=30):
    started=time.perf_counter()
    context=query.get('context','');today=date.fromisoformat(query.get('as_of','2026-11-04')[:10])
    plan=plan_recall(db,model,query['text'],context,depth=depth,today=today,timeout=timeout)
    result=db.recall(query['text'],context,max_items=5,max_chars=1800,depth=depth,today=today,semantic_plan=plan)
    result['plan']=plan;result['latency_ms']=(time.perf_counter()-started)*1000
    return result


def regression(output,model,dev=False):
    corpus=json.loads((ROOT/'evals/chinese_memory_cases.baseline.json').read_text());memory=json.loads((OLD/'memory.json').read_text())
    before=load_old_class(output)(output/'before.sqlite3');populate(before,corpus,memory)
    after=MemoryDB(output/'after.sqlite3');populate(after,corpus,memory)
    report={'dataset':'old_regression','fixture_sha256':hashlib.sha256((ROOT/'evals/chinese_memory_cases.baseline.json').read_bytes()).hexdigest()}
    try:
        queries=[q for q in corpus['queries'] if q['query_id'] in (['q03','q12'] if dev else [q['query_id'] for q in corpus['queries']])]
        if not dev:report['relation_reviews']=audit_existing(after,model)
        prior={str(d):{} for d in [0,1,2]};current={str(d):{} for d in [0,1,2]}
        for q in queries:
            for depth in ([0] if dev else [0,1,2]):
                prior[str(depth)][q['query_id']]=before.recall(q['text'],q.get('context',''),depth=depth,today=date.fromisoformat(q['as_of'][:10]),max_chars=1800)
        tasks=[(q,d) for q in queries for d in ([0] if dev else [0,1,2])]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={pool.submit(apply_plan,after,model,q,d):(q,d) for q,d in tasks}
            for future in as_completed(futures):
                q,depth=futures[future]
                try:current[str(depth)][q['query_id']]=future.result()
                except Exception as err:current[str(depth)][q['query_id']]={'error_type':type(err).__name__,'results':[],'injection':''}
                dump(output/'partial.json',current)
                print(json.dumps({'query':q['query_id'],'depth':depth,'done':len(model.calls)}),flush=True)
        report.update({'before':prior,'after':current,'memory':export_database(after)})
        report['scores']={d:score_run(corpus,memory['summaries'],memory['profiles'],memory['links'],items) for d,items in current.items()}
        # Reproduce the actual real DSH paraphrased checkpoint from phase B.
        old_live=json.loads((ROOT/'evals/results/deepseek-flash-2026-11-04/dsh-live-final/report.json').read_text())
        context='\n'.join(block.get('text','') for msg in old_live['compaction']['after_messages'] for block in msg.get('content',[]) if block.get('type')=='text')
        live_db=MemoryDB(output/'live-replay.sqlite3')
        try:
            for i,s in enumerate(old_live['consolidated_corpus']['summaries']):
                mid=live_db.add_message('live','user',s['text'],message_id='live:'+str(i));live_db.add_summary(s['text'],[mid],s.get('start_time'),s.get('end_time'),summary_id=s['id'])
            report['real_checkpoint_replay']=apply_plan(live_db,model,{'text':'旧码头那杯桂花拿铁的杯套编号是什么？蓝色地铁票放在哪里？','context':context},0)
        finally:live_db.close()
    finally:before.close();after.close()
    return report


def holdout(output,model):
    path=ROOT/'evals/holdout/chinese_sealed_v1.json';corpus=json.loads(path.read_text())
    db=MemoryDB(output/'heldout.sqlite3');report={'dataset':'heldout_once','fixture_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'coverage':[],'relations':[]}
    try:
        for session in corpus['sessions']:
            for msg in session['messages']:db.add_message(session['session_id'],msg['role'],msg['text'],msg['event_time'],msg['id'],msg['source'])
            consolidate_session(db,session['session_id'],model)
        memory=export_database(db);report['memory']=memory
        # Baseline runs on the same REAL extraction outputs, without retuning.
        before=load_old_class(output)(output/'before-heldout.sqlite3');populate(before,corpus,memory)
        report['before']={str(d):{q['query_id']:before.recall(q['text'],depth=d,max_chars=1800,today=date(2026,11,4)) for q in corpus['queries']} for d in [0,1,2]};before.close()
        after={str(d):{} for d in [0,1,2]}
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks={pool.submit(apply_plan,db,model,q,d):(q,d) for q in corpus['queries'] for d in [0,1,2]}
            for future in as_completed(tasks):
                q,d=tasks[future]
                try:after[str(d)][q['query_id']]=future.result()
                except Exception as err:after[str(d)][q['query_id']]={'error_type':type(err).__name__,'results':[],'injection':''}
                print(json.dumps({'heldout':q['query_id'],'depth':d}),flush=True)
        report['after']=after
        report['scores']={d:score_run(corpus,memory['summaries'],memory['profiles'],memory['links'],r) for d,r in after.items()}
        for case in corpus['coverage_cases']:
            data={'query':'完整判断这段记忆的覆盖','context':case['context'],'candidates':[{'id':'target','text':case['memory']}], 'max_selected':1}
            try:
                raw,meta=model.decide(RECALL_PROMPT+'\n本次必须selected中含target，即使full，不做相关性筛选。',data,purpose='coverage_holdout',timeout=30)
                result=validate_coverage(case['memory'],case['context'],raw['selected'][0]);actual=result['state'];bucket='missing' if actual=='contradicted' else actual
                report['coverage'].append({'case_id':case['case_id'],'expected':case['expected'],'result':result,'rule_outcome':'confirmed_correct_against_frozen_label' if bucket==case['expected'] else 'unresolved' if actual=='uncertain' else 'confirmed_error_against_frozen_label'})
            except Exception as err:report['coverage'].append({'case_id':case['case_id'],'error_type':type(err).__name__,'rule_outcome':'unresolved'})
        facts={f['fact_id']:f for f in corpus['gold_facts']};sources={m['id']:m for s in corpus['sessions'] for m in s['messages']}
        for case in corpus['relation_cases']:
            def endpoint(side):
                fid=case.get(side+'_fact_id')
                if fid:
                    mid=facts[fid]['evidence_ids'][0];return {'text':sources[mid]['text'],'start_time':facts[fid]['time_start'],'evidence_ids':[mid]}
                mid=case['case_id']+side;sources[mid]={'id':mid,'text':case[side+'_text']};return {'text':case[side+'_text'],'evidence_ids':[mid]}
            c={'id':case['case_id'],'kind':case['proposed_kind'],'left':endpoint('left'),'right':endpoint('right')}
            rows,_,_=review_relations(model,[c],sources,timeout=30);r=rows[0]
            # An invalid proposal may be safely retagged to a supported canonical
            # type. It never counts as keeping the wrong proposed type.
            ok=(r['status']=='rejected' or r['status']=='supported' and r['kind'] in case['expected_kinds']) if case['expected_status']=='rejected' else r['status']=='supported' and r['kind'] in case['expected_kinds']
            report['relations'].append({'case':case,'review':r,'rule_outcome':'confirmed_correct_against_frozen_label' if ok else 'unresolved' if r['status']=='uncertain' else 'confirmed_error_against_frozen_label'})
    finally:db.close()
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['dev','regression','holdout']);p.add_argument('--output',type=Path,required=True);p.add_argument('--budget',type=int,default=160);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    model=MeteredModel(args.budget)
    if not model.configured:raise SystemExit('Explicit real model configuration required; no mock fallback')
    started=time.perf_counter();report={}
    try:report=holdout(args.output,model) if args.mode=='holdout' else regression(args.output,model,args.mode=='dev')
    except Exception as err:report.update({'fatal_error_type':type(err).__name__});raise
    finally:
        dump(args.output/'calls.json',model.calls)
        report.update({'elapsed_s':time.perf_counter()-started,'model':model.cfg.public(),'calls':len(model.calls),'usage_actual':{k:sum((c.get('measurement') or {}).get('usage',{}).get(k,0) if (c.get('measurement') or {}).get('usage') else 0 for c in model.calls) for k in ['prompt_tokens','completion_tokens','total_tokens']},'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['goldfish/semantic.py','goldfish/relations.py','goldfish/engine.py','goldfish/consolidate.py']},'validation_provenance':'frozen gold rules or Codex review; model verdicts never labelled human verification'})
        dump(args.output/'report.json',report)
        print(json.dumps({'report':str(args.output/'report.json'),'calls':len(model.calls),'usage_actual':report['usage_actual']}),flush=True)

if __name__=='__main__':main()
