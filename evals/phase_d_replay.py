"""Supplemental source-output replay, baseline latency, and real cache probe."""
from datetime import date
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch
import os

from goldfish.consolidate import consolidate_session
from goldfish.engine import MemoryDB
from .phase_d import ROOT,before_class,make_service,MeteredEmbedding
from .phase_c import MeteredModel
from .run_real import dump,export_database


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    result={'notice':'Replay uses a recorded real phase C response. No fresh LLM call, no synthetic response claimed as real inference.',
            'replay':{},'baseline_latency':{}}
    raw=json.loads((ROOT/'evals/results/phase-c-deepseek-flash-2026-11-04/heldout-once/calls.json').read_text())[3]['output']
    fixture=json.loads((ROOT/'evals/holdout/chinese_sealed_v1.json').read_text());session=next(s for s in fixture['sessions'] if s['session_id']=='h03')
    path=args.output/'old_consolidate.py';path.write_bytes(subprocess.check_output(['git','show','1a19a00:goldfish/consolidate.py'],cwd=ROOT))
    spec=importlib.util.spec_from_file_location('goldfish.phase_d_old_consolidate',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Recorded:
        def extract(self,*a):return raw
    for name,handler in [('before',module.consolidate_session),('after',consolidate_session)]:
        db=MemoryDB(args.output/(name+'.sqlite3'))
        try:
            for m in session['messages']:db.add_message('h03',m['role'],m['text'],m['event_time'],m['id'],m['source'])
            row={}
            try:row['result']=handler(db,'h03',Recorded())
            except Exception as err:row['error_type']=type(err).__name__
            row['memory']=export_database(db);row['unprocessed']=len(db.get_unprocessed_messages('h03'))
            row['quarantined']=[dict(r) for r in db.db.execute('SELECT * FROM consolidation_rejections')]
            result['replay'][name]=row
        finally:db.close()
    old=before_class(args.output)
    for dataset in ('regression','phase_c_regression','heldout','heldout_growth'):
        stored=json.loads((args.root/(dataset+'.json')).read_text());db=old(args.root/(dataset+'-before.sqlite3'));times={}
        try:
            for q in stored['queries']:
                start=time.perf_counter();r=db.recall(q['text'],q.get('context',''),max_items=3,max_chars=1800,depth=0,today=date(2026,11,4))
                times[q['query_id']]={'latency_ms':(time.perf_counter()-start)*1000,'same_summary_ids_as_original':
                    [x['id'] for x in r['results']]==[x['id'] for x in stored['variants']['existing'][q['query_id']]['results']]}
        finally:db.close()
        result['baseline_latency'][dataset]=times
    model=MeteredModel(0);embedding=MeteredEmbedding();service=make_service(args.root/'heldout_growth.sqlite3',model,embedding)
    try:
        result['cache_probe']=[]
        with patch.dict(os.environ,{'GOLDFISH_RETRIEVAL_MODE':'hybrid','GOLDFISH_SEMANTIC_RECALL':'0'}):
            for _ in range(2):
                row=service.handle('/recall',{'query':'为什么前几年没去临港，今年却愿意试一段？','context':'','limit':3})
                result['cache_probe'].append(row['retrieval'])
        result['embedding_calls']=embedding.calls
    finally:service.close()
    dump(args.output/'report.json',result)
    print(json.dumps({'replay_counts':{k:len(v['memory']['summaries']) for k,v in result['replay'].items()},
                      'actual_cache_probe_embedding_calls':len(embedding.calls)}))


if __name__=='__main__':main()
