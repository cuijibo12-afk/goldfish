"""Real phase D four-way retrieval comparison; no synthetic model fallback."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import date
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from goldfish.consolidate import consolidate_session
from goldfish.engine import MemoryDB
from goldfish.providers import EmbeddingModel
from goldfish.server import Service
from .phase_c import MeteredModel,populate
from .run_real import export_database,dump
from .scoring import score_run

ROOT=Path(__file__).resolve().parents[1]


class MeteredEmbedding(EmbeddingModel):
    def __init__(self):super().__init__();self.calls=[]
    def embed(self,texts,**kwargs):
        row={'inputs':texts,'purpose':kwargs.get('purpose','document')};self.calls.append(row)
        try:
            vectors=super().embed(texts,**kwargs)
            row['vectors_sha256']=hashlib.sha256(json.dumps(vectors).encode()).hexdigest()
            return vectors
        except Exception as err:row['error_type']=type(err).__name__;raise
        finally:row['measurement']=self.last_call


def before_class(output):
    path=output/'before_engine.py';path.write_bytes(subprocess.check_output(['git','show','1a19a00:goldfish/engine.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('goldfish.phase_d_before',path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return m.MemoryDB


def add_noise(db,count):
    topics=[('厨房','量杯','橱柜'),('园艺','花盆','阳台'),('交通','车票','车站'),('照明','台灯','书桌'),('文具','铅笔','抽屉'),('运动','球拍','球馆'),('图书','书签','书架')]
    with db.transaction():
        for i in range(count):
            t,item,place=topics[i%len(topics)];year=2023+i%4
            text=f'{year}年{1+i%12}月{1+i%27}日，用户整理了{place}的{item}，标签为NX-{10000+i}；这是日常{t}记录，没有发生其他事情。'
            mid=db.add_message('noise','user',text,message_id=f'noise-m-{i}')
            sid=db.add_summary(text,[mid],f'{year}-{1+i%12:02}-{1+i%27:02}',summary_id=f'noise-s-{i}')
            db.attach_theme(db.add_theme(t),sid,'日常物品整理')


def make_service(path,model,embedding):
    # Attach metered providers before any index/model task can run.
    with patch.dict(os.environ,{'GOLDFISH_LLM_PROVIDER':'','GOLDFISH_LLM_MODEL':'','GOLDFISH_LLM_URL':'',
                                'GOLDFISH_EMBED_PROVIDER':'','GOLDFISH_EMBED_MODEL':'','GOLDFISH_EMBED_URL':''}):
        service=Service(path)
    service.chat=model;service.embeddings=embedding
    return service


def reindex(service):
    before=len(service.embeddings.calls);started=time.perf_counter();total=0
    while True:
        n=service._reindex(max_batches=16)['indexed'];total+=n
        if not n:break
    return {'indexed':total,'calls':len(service.embeddings.calls)-before,'elapsed_ms':(time.perf_counter()-started)*1000}


def evaluate(name,corpus,memory,output,model,embedding,noise=0,dev=False):
    service=make_service(output/(name+'.sqlite3'),model,embedding);old=before_class(output)(output/(name+'-before.sqlite3'))
    report={'name':name,'noise_count':noise,'variants':{},'scores':{},'queries':corpus['queries']}
    try:
        populate(service.db,corpus,memory);populate(old,corpus,memory)
        for s in corpus['sessions']:service.db.mark_processed(s['session_id'],max(m['seq'] for m in service.db.get_unprocessed_messages(s['session_id'],limit=10000)))
        if noise:add_noise(service.db,noise);add_noise(old,noise)
        report['index_build']=reindex(service)
        actual=export_database(service.db);report['memory']=actual
        with gzip.open(output/(name+'-vectors.json.gz'),'wt') as f:
            json.dump([dict(r) for r in service.db.db.execute('SELECT * FROM summary_vectors')],f)
        queries=corpus['queries']
        if dev:queries=[q for q in queries if q['query_id'] in {'q03','q13','q14','q22','q19'}]
        report['variants']['existing']={q['query_id']:old.recall(q['text'],q.get('context',''),max_items=3,max_chars=1800,depth=0,today=date(2026,11,4)) for q in queries}
        for mode in ('embedding','hybrid','hybrid_llm'):
            service._query_vectors.clear()  # Separate actual cold-query costs per variant.
            results={};first_chat=len(model.calls);first_embed=len(embedding.calls)
            with patch.dict(os.environ,{'GOLDFISH_RETRIEVAL_MODE':mode,'GOLDFISH_SEMANTIC_RECALL':'1'}):
                def run(q):
                    start=time.perf_counter();r=service.handle('/recall',{'query':q['text'],'context':q.get('context',''),'limit':3,'max_chars':1800,'depth':0})
                    r['latency_ms']=(time.perf_counter()-start)*1000;return r
                with ThreadPoolExecutor(max_workers=2) as pool:
                    tasks={pool.submit(run,q):q['query_id'] for q in queries}
                    for future in as_completed(tasks):
                        qid=tasks[future]
                        try:results[qid]=future.result()
                        except Exception as err:results[qid]={'results':[],'injection':'','error_type':type(err).__name__}
                        print(json.dumps({'dataset':name,'mode':mode,'query':qid}),flush=True)
            report['variants'][mode]=results
            report.setdefault('actual_calls_by_variant',{})[mode]={'llm':len(model.calls)-first_chat,'embedding':len(embedding.calls)-first_embed}
        for mode,rows in report['variants'].items():
            report['scores'][mode]=score_run(corpus,actual['summaries'],actual['profiles'],actual['links'],rows)
        dump(output/(name+'.json'),report)
    finally:old.close();service.close()
    return report


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--dev',action='store_true');parser.add_argument('--budget',type=int,default=120)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    model=MeteredModel(args.budget);embedding=MeteredEmbedding()
    if not model.configured or not embedding.configured:raise SystemExit('Explicit real LLM and embedding configuration required')
    report={'models':{'llm':model.cfg.public(),'embedding':embedding.cfg.public()},'datasets':{},'source_hashes':{
        str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'goldfish').glob('*.py')}}
    try:
        c=json.loads((ROOT/'evals/chinese_memory_cases.baseline.json').read_text());m=json.loads((ROOT/'evals/results/deepseek-flash-2026-11-04/validated/memory.json').read_text())
        r=evaluate('regression',c,m,args.output,model,embedding,dev=args.dev);report['datasets']['regression']='regression.json'
        if not args.dev:
            c=json.loads((ROOT/'evals/holdout/chinese_sealed_v1.json').read_text());m=json.loads((ROOT/'evals/results/phase-c-deepseek-flash-2026-11-04/heldout-once/report.json').read_text())['memory']
            evaluate('phase_c_regression',c,m,args.output,model,embedding);report['datasets']['phase_c_regression']='phase_c_regression.json'
            c=json.loads((ROOT/'evals/holdout/chinese_sealed_v2_phase_d.json').read_text());db=MemoryDB(args.output/'extraction.sqlite3')
            extraction=[]
            try:
                for session in c['sessions']:
                    for msg in session['messages']:db.add_message(session['session_id'],msg['role'],msg['text'],msg['event_time'],msg['id'],msg['source'])
                    try:extraction.append({'session':session['session_id'],'result':consolidate_session(db,session['session_id'],model)})
                    except Exception as err:extraction.append({'session':session['session_id'],'error_type':type(err).__name__})
                m=export_database(db);report['heldout_extraction']=extraction
                dump(args.output/'heldout-extraction.json',m)
            finally:db.close()
            evaluate('heldout',c,m,args.output,model,embedding);report['datasets']['heldout']='heldout.json'
            evaluate('heldout_growth',c,m,args.output,model,embedding,noise=1000);report['datasets']['heldout_growth']='heldout_growth.json'
    except Exception as err:
        report['fatal_error_type']=type(err).__name__;raise
    finally:
        dump(args.output/'llm-calls.json',model.calls);dump(args.output/'embedding-calls.json',embedding.calls)
        report['usage_actual']={kind:{k:sum((r.get('measurement') or {}).get('usage',{}).get(k,0) if (r.get('measurement') or {}).get('usage') else 0 for r in rows) for k in ('prompt_tokens','completion_tokens','total_tokens')} for kind,rows in [('llm',model.calls),('embedding',embedding.calls)]}
        report['call_counts']={'llm':len(model.calls),'embedding':len(embedding.calls)}
        report['fixture_sha256']=hashlib.sha256((ROOT/'evals/holdout/chinese_sealed_v2_phase_d.json').read_bytes()).hexdigest()
        dump(args.output/'report.json',report);print(json.dumps({'calls':report['call_counts'],'usage':report['usage_actual']}),flush=True)


if __name__=='__main__':main()
