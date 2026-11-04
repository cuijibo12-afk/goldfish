"""Re-render stored plans offline and audit scoring against actual source IDs.

No decisions or gold labels are changed. Stress distractors must be included
in the source catalog, otherwise valid distractor IDs look like fake evidence.
"""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3

from goldfish.engine import MemoryDB
from .scoring import score_run
from .run_real import dump

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(__doc__);parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    analysis={'notice':'Offline rendering fix and source-catalog scoring correction; no new model response or gold-label tuning.',
        'renderer_sha256':hashlib.sha256((ROOT/'goldfish/engine.py').read_bytes()).hexdigest(),'datasets':{}}
    for ds,fixture in [('regression','chinese_memory_cases.baseline.json'),('phase_c_regression','holdout/chinese_sealed_v1.json'),
                       ('heldout','holdout/chinese_sealed_v2_phase_d.json'),('heldout_growth','holdout/chinese_sealed_v2_phase_d.json')]:
        source=json.loads((args.root/(ds+'.json')).read_text());corpus=json.loads((ROOT/'evals'/fixture).read_text())
        db=MemoryDB(args.root/(ds+'.sqlite3'))
        try:
            corpus['raw_messages']=[dict(r) for r in db.db.execute('SELECT * FROM raw_messages')]
            variants={};changes=[];metrics={};memory=source['memory']
            for mode,rows in source['variants'].items():
                variants[mode]={}
                for q in source['queries']:
                    old=rows[q['query_id']];plan=old.get('semantic_plan')
                    if mode=='existing' or not plan:new=old
                    else:
                        new=db.recall(q['text'],q.get('context',''),max_items=3,max_chars=1800,depth=0,today=date(2026,11,4),semantic_plan=plan)
                        new.update(retrieval=old.get('retrieval'),latency_ms=old.get('latency_ms'),semantic_plan=plan)
                    if [r['id'] for r in old['results']]!=[r['id'] for r in new['results']] or old.get('profile_ids')!=new.get('profile_ids'):
                        changes.append({'mode':mode,'query':q['query_id'],'before_ids':[r['id'] for r in old['results']],'after_ids':[r['id'] for r in new['results']]})
                    variants[mode][q['query_id']]=new
                score=score_run(corpus,memory['summaries'],memory['profiles'],memory['links'],variants[mode])
                metrics[mode]={'summary':score['recall']['summary'],'queries':score['recall']['queries'],
                    'invalid_evidence_record_count':len(score['extraction']['invalid_evidence_record_ids']),
                    'extraction':{k:score['extraction'][k] for k in ('gold_count','record_count','probe_matched_count','manual_review_ids','no_evidence_candidate_ids')},
                    'profiles':score['profiles']}
            dump(args.output/(ds+'.json'),{'queries':source['queries'],'variants':variants,'rendering_only':True})
            analysis['datasets'][ds]={'metrics':metrics,'rendered_selection_changes':changes,
                'original_invalid_evidence_count':len(source['scores']['existing']['extraction']['invalid_evidence_record_ids']),
                'source_catalog_count':len(corpus['raw_messages']),'fixture_sha256':hashlib.sha256((ROOT/'evals'/fixture).read_bytes()).hexdigest()}
        finally:db.close()
    dump(args.output/'analysis.json',analysis)
    print(json.dumps({'rendered_selection_changes':{k:v['rendered_selection_changes'] for k,v in analysis['datasets'].items()}}))


if __name__=='__main__':main()
