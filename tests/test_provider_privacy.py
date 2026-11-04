"""Provider privacy and malformed-response checks; no real model or cloud calls."""
import math
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from goldfish.providers import ChatModel, EmbeddingModel, ModelConfig, _check_url, _post_json


class ProviderPrivacyTests(unittest.TestCase):
    def test_redirect_never_contacts_unconfigured_destination(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', '0')))
                if self.path.startswith('/redirect/'):
                    self.send_response(int(self.path.rsplit('/', 1)[1]))
                    self.send_header('Location', f'http://127.0.0.1:{self.server.server_port}/unconfigured')
                elif self.path == '/error':
                    self.send_response(500, 'synthetic-private-server-reason')
                else:
                    requests.append(('POST', self.headers.get('Authorization')))
                    self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def do_GET(self):
                requests.append(('GET', self.headers.get('Authorization')))
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            for status in (301, 302, 303, 307, 308):
                with self.subTest(status=status), self.assertRaisesRegex(ValueError, 'redirects are disabled'):
                    _post_json(f'http://127.0.0.1:{httpd.server_port}/redirect/{status}',
                               {'text': 'synthetic private test text'}, 'synthetic-key')
            with self.assertRaisesRegex(RuntimeError, '^Model endpoint returned HTTP 500$'):
                _post_json(f'http://127.0.0.1:{httpd.server_port}/error', {}, 'synthetic-key')
            self.assertEqual(requests, [])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()

    def test_disabled_models_do_not_open_network(self):
        with patch.dict(os.environ, {}, clear=True), patch('goldfish.providers.build_opener') as opener:
            with self.assertRaises(RuntimeError):
                ChatModel().extract([], [])
            with self.assertRaises(RuntimeError):
                EmbeddingModel().embed(['synthetic text'])
            opener.assert_not_called()

    def test_endpoint_credentials_rejected_and_key_not_in_repr(self):
        with self.assertRaisesRegex(ValueError, 'credentials'):
            _check_url('https://user:synthetic-key@example.com/v1')
        cfg = ModelConfig('LLM', 'openai', 'https://example.com/v1', 'model', 'synthetic-key')
        self.assertNotIn('synthetic-key', repr(cfg))
        self.assertNotIn('synthetic-key', str(cfg.public()))

    def test_embedding_indices_must_exactly_match_inputs(self):
        model = EmbeddingModel(ModelConfig('EMBED', 'openai', 'https://example.com/v1', 'embed'))
        for indices in ([0, 0], [1, 2], [-1, 0], [False, 1], ['0', 1]):
            data = {'data': [{'index': index, 'embedding': [1.0, 0.0]} for index in indices]}
            with self.subTest(indices=indices), patch('goldfish.providers._post_json', return_value=data):
                with self.assertRaisesRegex(ValueError, 'indices'):
                    model.embed(['first memory', 'second memory'])

    def test_embedding_invalid_vectors_rejected_without_echoing_private_values(self):
        model = EmbeddingModel(ModelConfig('EMBED', 'ollama', 'http://127.0.0.1:11434', 'embed'))
        for vectors in ([[1.0], [1.0, 0.0]], [[True, 0.0], [1.0, 0.0]],
                        [['synthetic-private-text'], [1.0]], [[float('nan')], [1.0]]):
            with self.subTest(vectors=vectors), patch('goldfish.providers._post_json', return_value={'embeddings': vectors}):
                with self.assertRaises(ValueError) as error:
                    model.embed(['first memory', 'second memory'])
                self.assertNotIn('synthetic-private-text', str(error.exception))

    def test_embedding_normalization_handles_large_finite_values(self):
        model = EmbeddingModel(ModelConfig('EMBED', 'ollama', 'http://127.0.0.1:11434', 'embed'))
        with patch('goldfish.providers._post_json', return_value={'embeddings': [[1e308, 1e308]]}):
            vector = model.embed(['synthetic text'])[0]
        self.assertAlmostEqual(sum(n*n for n in vector), 1.0)
        self.assertTrue(all(math.isfinite(n) for n in vector))


if __name__ == '__main__':
    unittest.main()
