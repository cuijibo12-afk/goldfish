"""Real marginal main-model input usage; one-token outputs are not quality tests."""
import argparse
import json
from pathlib import Path
import time

from goldfish.providers import _endpoint,_post_json,load_config


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--reuse-controls',type=Path)
    args=p.parse_args();args.output.mkdir(parents=True,exist_ok=False);cfg=load_config('LLM')
    if not cfg.configured:raise SystemExit('Explicit actual model required')
    result={'purpose':'input_usage_only_not_answer_quality','calls':[],'comparisons':[],'sample_selection':'fixed before viewing new-set outputs'}
    controls=json.loads(args.reuse_controls.read_text()) if args.reuse_controls else None
    if controls:result['reused_controls_from']=str(args.reuse_controls)
    try:
        for dataset,ids in [('regression',['q03','q13','q14','q22']),('heldout',['w01','w03','w05','w09'])]:
            source=json.loads((args.root/(dataset+'.json')).read_text());queries={q['query_id']:q for q in source['queries']}
            for qid in ids:
                baseline=next(c['response']['usage']['prompt_tokens'] for c in controls['calls'] if c['dataset']==dataset and c['query_id']==qid and c['variant']=='no_memory') if controls else None
                variants=[('no_memory','')]+[(variant,rows[qid]['injection']) for variant,rows in source['variants'].items()]
                for variant,injection in variants:
                    if controls and variant in {'no_memory','existing'}:
                        prior=next(c for c in controls['comparisons'] if c['dataset']==dataset and c['query_id']==qid and c['variant']==variant)
                        result['comparisons'].append({**prior,'reused_control':True});continue
                    messages=[{'role':'system','content':'根据给出的来源回答用户，不推断心理。'},{'role':'user','content':queries[qid]['text']}]
                    if injection:messages.append({'role':'user','content':injection})
                    payload={'model':cfg.model,'messages':messages,'max_tokens':1,'thinking':{'type':'disabled'},'temperature':.1}
                    row={'dataset':dataset,'query_id':qid,'variant':variant,'request':payload};result['calls'].append(row);start=time.perf_counter()
                    try:
                        response=_post_json(_endpoint(cfg.endpoint,'/v1/chat/completions'),payload,cfg.key,timeout=30);row['response']=response
                        n=response['usage']['prompt_tokens']
                        if variant=='no_memory':baseline=n
                        result['comparisons'].append({'dataset':dataset,'query_id':qid,'variant':variant,'marginal_prompt_tokens':n-baseline if baseline is not None else None})
                    except Exception as err:row['error_type']=type(err).__name__
                    finally:row['latency_ms']=(time.perf_counter()-start)*1000
    finally:
        result['usage_actual']={k:sum(c.get('response',{}).get('usage',{}).get(k,0) for c in result['calls']) for k in ('prompt_tokens','completion_tokens','total_tokens')}
        (args.output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'calls':len(result['calls']),'usage':result['usage_actual']}))


if __name__=='__main__':main()
