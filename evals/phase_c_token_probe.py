"""Measure marginal main-model input tokens using provider usage, not chars.

Outputs are deliberately capped at one token. These calls measure input cost;
they are never counted as successful answers or memory-quality evaluations.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from goldfish.providers import _endpoint, _post_json, load_config


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    cfg = load_config('LLM')
    if not cfg.configured or cfg.provider != 'openai':
        raise SystemExit('An explicitly configured real OpenAI-compatible model is required')
    args.output.mkdir(parents=True, exist_ok=False)
    project = Path(__file__).resolve().parents[1]
    report = {'purpose': 'input_token_measurement_only', 'model': cfg.public(), 'calls': [], 'comparisons': []}
    try:
        for dataset, fixture, qid in [
            ('regression', 'evals/chinese_memory_cases.baseline.json', 'q03'),
            ('heldout-once', 'evals/holdout/chinese_sealed_v1.json', 'z03'),
        ]:
            source = json.loads((args.root / dataset / 'report.json').read_text())
            query = next(q for q in json.loads((project / fixture).read_text())['queries'] if q['query_id'] == qid)
            base = [{'role': 'system', 'content': '根据给出的来源回答用户，不推断心理。'},
                    {'role': 'user', 'content': query['text']}]
            variants = [('no_memory', '')] + [('before_depth0', source['before']['0'][qid]['injection'])] + [
                ('after_depth' + str(d), source['after'][str(d)][qid]['injection']) for d in (0, 1, 2)]
            baseline = None
            for label, injection in variants:
                messages = base + ([{'role': 'user', 'content': injection}] if injection else [])
                request = {'model': cfg.model, 'messages': messages, 'max_tokens': 1, 'temperature': .1}
                if cfg.thinking:
                    request['thinking'] = {'type': cfg.thinking}
                row = {'dataset': dataset, 'query_id': qid, 'variant': label, 'request': request}
                report['calls'].append(row)
                started = time.perf_counter()
                try:
                    response = _post_json(_endpoint(cfg.endpoint, '/v1/chat/completions'), request, cfg.key, timeout=30)
                    row['response'] = response
                    tokens = response['usage']['prompt_tokens']
                    if label == 'no_memory':
                        baseline = tokens
                    report['comparisons'].append({'dataset': dataset, 'query_id': qid, 'variant': label,
                        'prompt_tokens': tokens, 'marginal_prompt_tokens': tokens - baseline if baseline is not None else None,
                        'injection_characters': len(injection)})
                except Exception as err:
                    row['error_type'] = type(err).__name__
                finally:
                    row['latency_ms'] = round((time.perf_counter() - started) * 1000, 3)
    finally:
        report['usage_actual'] = {key: sum(row.get('response', {}).get('usage', {}).get(key, 0)
            for row in report['calls']) for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')}
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'calls': len(report['calls']), 'usage_actual': report['usage_actual'],
                          'comparisons': report['comparisons']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
