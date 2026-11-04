"""End-to-end with a fake LOCAL model server; never calls a real API."""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from goldfish.server import Service


class FakeModel(BaseHTTPRequestHandler):
    def log_message(self,*args): pass

    def do_POST(self):
        length = int(self.headers['Content-Length'])
        data = json.loads(self.rfile.read(length).decode())
        if self.path == '/api/chat':
            inputs = json.loads(data['messages'][1]['content'])
            raw = inputs['raw_messages']
            result = {'summaries': [{'ref':'n1','text':'对职业变化有未知感',
                'evidence_ids':[raw[0]['id']], 'theme':'个人选择',
                'theme_description':'个人职业选择的经历'}],
                'links':[], 'profiles': []}
            result={'message':{'content':json.dumps(result,ensure_ascii=False)}}
        elif self.path == '/api/embed':
            texts=data['input']
            # Deterministic vectors: deliberately unify disparate phrasings.
            result={'embeddings':[[1.0,0.0] for _ in texts]}
        else:
            self.send_error(404)
            return
        payload=json.dumps(result).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class EndToEndTests(unittest.TestCase):
    def test_ollama_provider_integration(self):
        httpd=ThreadingHTTPServer(('127.0.0.1',0),FakeModel)
        th=threading.Thread(target=httpd.serve_forever,daemon=True)
        th.start()
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{
            'GOLDFISH_LLM_PROVIDER':'ollama', 'GOLDFISH_LLM_MODEL':'test-chat',
            'GOLDFISH_LLM_URL':f'http://127.0.0.1:{httpd.server_port}',
            'GOLDFISH_EMBED_PROVIDER':'ollama', 'GOLDFISH_EMBED_MODEL':'test-embed',
            'GOLDFISH_EMBED_URL':f'http://127.0.0.1:{httpd.server_port}'},clear=True):
            svc=Service(Path(tmp)/'goldfish.db')
            try:
                svc.handle('/ingest',{'session_id':'s','role':'user','text':'我有时候对陌生职业选择心存顾虑'})
                svc.handle('/ingest',{'session_id':'s','role':'assistant','text':'你可以描述一下具体顾虑'})
                response=svc.handle('/consolidate',{'session_id':'s'})
                self.assertTrue(response['ok'])
                self.assertEqual(response['summaries'],1)
                self.assertEqual(response['embedding']['indexed'],1)
                recalled=svc.handle('/recall',{'query':'我为什么害怕新的挑战','max_chars':1500})
                self.assertEqual(len(recalled['results']),1)
                self.assertIn('对职业变化',recalled['injection'])
                self.assertIn('semantic',recalled['mode'])
                self.assertTrue(svc.handle('/providers',{})['embedding']['local'])
            finally:
                svc.pool.shutdown(wait=True)
                svc.db.close()
        httpd.shutdown(); httpd.server_close()

if __name__=='__main__': unittest.main()
