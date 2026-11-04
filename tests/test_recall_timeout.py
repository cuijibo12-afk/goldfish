"""A slow local vector endpoint must leave time for lexical recall in DSH."""
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from goldfish.providers import EmbeddingModel, ModelConfig
from goldfish.server import Service


class RecallTimeoutTests(unittest.TestCase):
    def test_long_effective_context_keeps_fact_coverage_on_both_ends(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            service = Service(Path(tmp) / 'memory.db')
            try:
                fact = '在码头喜欢喝桂花拿铁'
                source = service.db.add_message('s', 'user', fact)
                service.db.add_summary(fact, [source])
                for context in (fact + 'x' * 80000, 'x' * 80000 + fact):
                    with self.subTest(fact_at_start=context.startswith(fact)):
                        result = service.handle('/recall', {'query': '码头桂花拿铁', 'context': context})
                        self.assertEqual(result['injection'], '')
            finally:
                service.close()

    def test_slow_embedding_falls_back_before_bridge_timeout(self):
        release = threading.Event()
        entered = threading.Event()

        class SlowEmbedding(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                entered.set()
                release.wait(3)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"embeddings":[[1,0]]}')
                except (BrokenPipeError, ConnectionResetError): pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), SlowEmbedding)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            service = Service(Path(tmp) / 'memory.db')
            try:
                source = service.db.add_message('s', 'user', '我在码头喜欢喝桂花拿铁')
                summary = service.db.add_summary('在码头喜欢喝桂花拿铁', [source])
                service.embeddings = EmbeddingModel(ModelConfig('EMBED', 'ollama',
                    f'http://127.0.0.1:{server.server_port}', 'synthetic-slow'))
                started = time.monotonic()
                result = service.handle('/recall', {'query': '码头桂花拿铁', 'semantic_timeout_ms': 80})
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertIn(summary, {row['id'] for row in result['results']})
            finally:
                release.set()
                service.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__': unittest.main()
