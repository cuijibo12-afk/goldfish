"""Offline phase D metrics and complete usage ledger, without credentials."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import statistics


def read(path):return json.loads(path.read_text())


def billed(usage):
    if not usage:return None
    hit=usage.get('prompt_cache_hit_tokens',usage.get('prompt_tokens_details',{}).get('cached_tokens'))
    if hit is None:return None
    miss=usage.get('prompt_cache_miss_tokens',usage['prompt_tokens']-hit)
    # Official observed peak USD rates, per 1M tokens; invoice not available.
    return (hit*.006+miss*.3+usage.get('completion_tokens',0)*1.2)/1000000


def stats(values):
    values=sorted(values)
    return {'median':statistics.median(values),'p95_nearest_rank':values[math.ceil(.95*len(values))-1],'max':max(values)} if values else None


def analyze(root):
    final=read(root/'rendered/analysis.json');replay=read(root/'replay/report.json');main=read(root/'frozen/report.json')
    result={'method':'Frozen evidence/phrase probes; not independent human semantic validation.',
            'datasets':{},'usage_ledger':[],'unknown_queue':[]}
    embedding_calls=read(root/'frozen/embedding-calls.json');cursor=0
    for ds in ('regression','phase_c_regression','heldout','heldout_growth'):
        source=read(root/'frozen'/ (ds+'.json'));rendered=read(root/'rendered'/(ds+'.json'))
        n=source['index_build']['calls'];index=embedding_calls[cursor:cursor+n];cursor+=n
        row={'index_build':{**source['index_build'],'embedding_tokens':sum(x['measurement']['usage']['total_tokens'] for x in index)},
             'source_catalog_correction':{k:final['datasets'][ds][k] for k in ('original_invalid_evidence_count','source_catalog_count')},'variants':{}}
        for mode,values in rendered['variants'].items():
            metrics=final['datasets'][ds]['metrics'][mode]
            measures=[m for r in values.values() for m in (r.get('retrieval') or {}).get('measurements',[])]
            lm=[m for m in measures if not m.get('purpose','').startswith('embedding_')]
            em=[m for m in measures if m.get('purpose','').startswith('embedding_')]
            if mode!='existing':cursor+=source['actual_calls_by_variant'][mode]['embedding']
            times=[r['latency_ms'] for r in source['variants'][mode].values() if r.get('latency_ms') is not None]
            if mode=='existing':times=[r['latency_ms'] for r in replay['baseline_latency'][ds].values()]
            row['variants'][mode]={'quality':metrics['summary'],'latency_ms':stats(times),
                'llm_calls':len(lm),'llm_token_usage':{k:sum((m.get('usage') or {}).get(k,0) for m in lm) for k in ('prompt_tokens','completion_tokens','total_tokens')},
                'llm_usd_at_observed_peak_rates':sum(billed(m.get('usage')) or 0 for m in lm),
                'embedding_calls':len(em),'embedding_tokens':sum((m.get('usage') or {}).get('total_tokens',0) for m in em),
                'model_purposes':dict(Counter(m.get('purpose') for m in lm)),
                'input_characters_not_tokens':sum(len(r['injection']) for r in values.values()),
                'candidates_max':max(((r.get('retrieval') or {}).get('candidate_count') or 0 for r in values.values()),default=0)}
            for q in metrics['queries']:
                if q.get('unknown_ids') or q.get('unresolved_fact_ids') or q.get('false_positive_ids') or q.get('no_candidate_fact_ids'):
                    result['unknown_queue'].append({'dataset':ds,'variant':mode,**{k:q[k] for k in ('query_id','unknown_ids','unresolved_fact_ids','false_positive_ids','no_candidate_fact_ids')}})
        result['datasets'][ds]=row
    assert cursor==len(embedding_calls)
    for name in ('dev','dev2','dev3','frozen'):
        report=read(root/name/'report.json');lm=read(root/name/'llm-calls.json');em=read(root/name/'embedding-calls.json')
        result['usage_ledger'].append({'run':name,'llm_calls':len(lm),'embedding_calls':len(em),'tokens':report['usage_actual'],
            'llm_usd_peak_estimate':sum(billed((r.get('measurement') or {}).get('usage')) or 0 for r in lm)})
    for name in ('live-native','live-lossy','live-native-final','live-lossy-final'):
        report=read(root/name/'report.json');lm=[r for r in report['calls'] if r['purpose']!='embedding'];em=[r for r in report['calls'] if r['purpose']=='embedding']
        result['usage_ledger'].append({'run':name,'llm_calls':len(lm),'embedding_calls':len(em),
            'tokens':{kind:{k:sum(r.get('response',{}).get('usage',{}).get(k,0) for r in rs) for k in ('prompt_tokens','completion_tokens','total_tokens')} for kind,rs in [('llm',lm),('embedding',em)]},
            'llm_usd_peak_estimate':sum(billed(r.get('response',{}).get('usage')) or 0 for r in lm)})
    for name in ('token-probe','token-probe-final'):
        report=read(root/name/'report.json');result['usage_ledger'].append({'run':name,'llm_calls':len(report['calls']),'embedding_calls':0,
            'tokens':{'llm':report['usage_actual'],'embedding':{}},'llm_usd_peak_estimate':sum(billed(r.get('response',{}).get('usage')) or 0 for r in report['calls'])})
    # Supplementary real local requests: initial console smoke and cache probe.
    result['usage_ledger'].append({'run':'cache_probe','llm_calls':0,'embedding_calls':len(replay['embedding_calls']),
        'tokens':{'llm':{},'embedding':{'total_tokens':sum(r['measurement']['usage']['total_tokens'] for r in replay['embedding_calls'])}},'llm_usd_peak_estimate':0})
    result['usage_ledger'].append({'run':'initial_real_local_smoke_console','llm_calls':0,'embedding_calls':1,
        'tokens':{'llm':{},'embedding':{'total_tokens':31}},'llm_usd_peak_estimate':0})
    result['total']={key:sum(r[key] for r in result['usage_ledger']) for key in ('llm_calls','embedding_calls','llm_usd_peak_estimate')}
    result['total']['llm_tokens']=sum(r['tokens']['llm'].get('total_tokens',0) for r in result['usage_ledger'])
    result['total']['embedding_tokens']=sum(r['tokens']['embedding'].get('total_tokens',0) for r in result['usage_ledger'])
    result['main_model_input_sample']=read(root/'token-probe-final/report.json')['comparisons']
    result['cache_probe']=replay['cache_probe']
    return result


def main():
    p=argparse.ArgumentParser(__doc__);p.add_argument('--root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();args.output.write_text(json.dumps(analyze(args.root),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':main()
