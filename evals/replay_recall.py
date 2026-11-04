"""Replay current retrieval over recorded REAL model memories, with zero API calls."""
import argparse
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from goldfish.engine import MemoryDB
from .scoring import score_run
from .run_real import export_database, dump, latency


def replay(run, output):
    corpus = json.loads((run / 'corpus.json').read_text())
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory() as tmp:
        copied = Path(tmp) / 'memory.sqlite3'
        source = sqlite3.connect((run / 'memory.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
        destination = sqlite3.connect(copied)
        try: source.backup(destination)
        finally: source.close(); destination.close()
        db = MemoryDB(copied)
        try:
            memory = export_database(db)
            recalls, graph_off, times = {}, {}, []
            for query in corpus['queries']:
                today = date.fromisoformat(query['as_of'][:10])
                start = time.perf_counter()
                recalls[query['query_id']] = db.recall(query['text'], query.get('context', ''),
                    max_items=5, max_chars=1800, depth=2, today=today)
                times.append((time.perf_counter() - start) * 1000)
                graph_off[query['query_id']] = db.recall(query['text'], query.get('context', ''),
                    max_items=5, max_chars=1800, depth=0, today=today)
            report = {'source_run': str(run), 'model_calls': 0,
                      'source_memory_sha256': hashlib.sha256((run / 'memory.json').read_bytes()).hexdigest(),
                      'fixture_sha256': hashlib.sha256((run / 'corpus.json').read_bytes()).hexdigest(),
                      'engine_sha256': hashlib.sha256(Path('goldfish/engine.py').read_bytes()).hexdigest(),
                      'recall_latency': latency(times),
                      'scores': score_run(corpus, memory['summaries'], memory['profiles'], memory['links'], recalls),
                      'graph_off_scores': score_run(corpus, memory['summaries'], memory['profiles'], memory['links'], graph_off)}
            dump(output / 'report.json', report)
            dump(output / 'recalls.json', {'graph_on': recalls, 'graph_off': graph_off})
            print(json.dumps({'report': str(output / 'report.json'), 'model_calls': 0,
                              'recall': report['scores']['recall']['summary']}, ensure_ascii=False))
        finally: db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    replay(args.run, args.output)


if __name__ == '__main__': main()
