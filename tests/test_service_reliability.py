"""Failure and concurrency regression tests using only local synthetic data."""
import contextlib
import io
import os
import json
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from goldfish.engine import MemoryDB
from goldfish.server import Service


class RecordingModel:
    configured=True

    def __init__(self, fail_count=0, block=False):
        self.calls=[]
        self.fail_count=fail_count
        self.entered=threading.Event()
        self.release=threading.Event()
        if not block: self.release.set()

    def extract(self,messages,related):
        self.calls.append([m['id'] for m in messages])
        self.entered.set()
        if not self.release.wait(5): raise TimeoutError('test model release timed out')
        if self.fail_count:
            self.fail_count-=1
            raise RuntimeError('PRIVATE_MODEL_RESPONSE_AND_KEY_MUST_NOT_BE_LOGGED')
        return {'summaries':[{'ref':'n1','text':'测试职业经历',
            'evidence_ids':[next(m['id'] for m in messages if m['role']=='user')]}],
            'profiles':[],'links':[]}


class ServiceReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.clean_env=patch.dict(os.environ,{},clear=True)
        self.clean_env.start()
        self.service=Service(Path(self.tmp.name)/'memory.db',retry_base=.02)

    def tearDown(self):
        self.service.close()
        self.clean_env.stop()
        self.tmp.cleanup()

    def ingest(self,count,session='s'):
        for index in range(count):
            self.service.handle('/ingest',{'session_id':session,
                'message_id':f'{session}:{index}',
                'role':'user' if index%2==0 else 'assistant',
                'text':f'第{index}条测试职业对话'})

    def wait_processed(self,session='s',timeout=5):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            with self.service.lock:
                remaining=self.service.db.get_unprocessed_messages(session)
            with self.service._state:
                idle=session not in self.service.pending and session not in self.service._retry_due
            if not remaining and idle: return
            time.sleep(.005)
        self.fail('background consolidation did not drain its durable source queue')

    def test_model_wait_does_not_hold_source_database_lock(self):
        model=RecordingModel(block=True)
        self.service.chat=model
        self.ingest(2)
        self.assertTrue(self.service.schedule('s'))
        self.assertTrue(model.entered.wait(2))
        try:
            with ThreadPoolExecutor(max_workers=1) as caller:
                future=caller.submit(self.service.handle,'/ingest',{
                    'session_id':'other','message_id':'other:0','role':'user','text':'仍可保存'})
                self.assertTrue(future.result(timeout=.5)['ok'])
        finally:
            model.release.set()
        self.wait_processed()

    def test_background_worker_drains_more_than_one_fourteen_message_batch(self):
        model=RecordingModel()
        self.service.chat=model
        self.ingest(31)
        self.service.schedule('s',force=True)
        self.wait_processed()
        self.assertEqual([len(batch) for batch in model.calls],[14,14,3])
        self.assertEqual(sum(len(batch) for batch in model.calls),31)
        self.assertEqual(self.service.handle('/stats',{})['summaries'],3)

    def test_transient_failure_retries_without_new_chat_and_redacts_logs(self):
        model=RecordingModel(fail_count=1)
        self.service.chat=model
        self.ingest(2)
        logs=io.StringIO()
        with contextlib.redirect_stdout(logs):
            self.service.schedule('s')
            self.wait_processed()
        self.assertEqual(len(model.calls),2)
        self.assertEqual(self.service.handle('/stats',{})['summaries'],1)
        self.assertNotIn('PRIVATE_MODEL_RESPONSE',logs.getvalue())
        self.assertIn('RuntimeError',logs.getvalue())
        with self.service.lock:
            self.assertEqual(self.service.db.db.execute(
                'SELECT last_error FROM consolidation_jobs WHERE session_id=?',('s',)).fetchone()[0],'')

    def test_restart_recovers_unprocessed_sources_without_new_chat(self):
        self.ingest(3)
        self.service.close()
        model=RecordingModel()
        with patch('goldfish.server.ChatModel',return_value=model):
            self.service=Service(Path(self.tmp.name)/'memory.db')
        self.wait_processed()
        self.assertEqual(len(model.calls),1)
        self.assertEqual(len(model.calls[0]),3)

    def test_concurrent_scheduling_submits_only_one_session_job(self):
        model=RecordingModel(block=True)
        self.service.chat=model
        self.ingest(2)
        try:
            with ThreadPoolExecutor(max_workers=12) as callers:
                decisions=list(callers.map(lambda _:self.service.schedule('s'),range(30)))
            self.assertEqual(sum(decisions),1)
            self.assertTrue(model.entered.wait(2))
        finally:
            model.release.set()
        self.wait_processed()
        self.assertEqual(len(model.calls),1)

    def test_close_waits_for_active_worker_before_closing_sqlite(self):
        model=RecordingModel(block=True)
        self.service.chat=model
        self.ingest(2)
        self.service.schedule('s')
        self.assertTrue(model.entered.wait(2))
        closed=threading.Event()
        closer=threading.Thread(target=lambda:(self.service.close(),closed.set()))
        closer.start()
        self.assertFalse(closed.wait(.05))
        model.release.set()
        closer.join(2)
        self.assertTrue(closed.is_set())
        recovered=MemoryDB(Path(self.tmp.name)/'memory.db')
        try:
            self.assertEqual(recovered.get_unprocessed_messages('s'),[])
        finally:
            recovered.close()

    def test_real_http_process_auth_idempotency_full_source_and_graceful_exit(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        database=Path(self.tmp.name)/'http-memory.db'
        env={**os.environ,'GOLDFISH_LOCAL_TOKEN':'synthetic-local-test-token'}
        process=subprocess.Popen([sys.executable,'-m','goldfish.server','--port',str(port),
            '--db',str(database)],cwd=Path(__file__).resolve().parents[1],env=env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def call(path,payload=None,token=True):
            headers={'Content-Type':'application/json'}
            if token: headers['X-Goldfish-Token']='synthetic-local-test-token'
            body=None if payload is None else json.dumps(payload).encode()
            with urlopen(Request(f'http://127.0.0.1:{port}'+path,data=body,headers=headers),timeout=1) as response:
                return json.loads(response.read())
        try:
            deadline=time.monotonic()+5
            while True:
                try:
                    self.assertTrue(call('/health')['ok'])
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        self.fail('isolated HTTP backend failed to start')
                    time.sleep(.01)
            with self.assertRaises(HTTPError) as denied: call('/stats',token=False)
            self.assertEqual(denied.exception.code,401)
            raw='原始长文本证据'*8000
            event={'session_id':'http','role':'user','message_id':'http:1','text':raw}
            self.assertEqual(call('/ingest',event)['message_id'],'http:1')
            self.assertEqual(call('/ingest',event)['message_id'],'http:1')
            self.assertEqual(call('/stats')['raw_messages'],1)
            with self.assertRaises(HTTPError) as conflict:
                call('/ingest',{**event,'text':'不能覆盖'})
            self.assertEqual(conflict.exception.code,400)
            with self.assertRaises(HTTPError) as invalid: call('/ingest',[])
            self.assertEqual(invalid.exception.code,400)
            self.assertTrue(call('/recall',{'query':'测试'})['ok'])
        finally:
            process.terminate()
            stdout,stderr=process.communicate(timeout=5)
        self.assertEqual(process.returncode,0,stderr)
        self.assertNotIn('原始长文本证据',stdout+stderr)
        restored=MemoryDB(database)
        try:
            self.assertEqual(restored.get_message('http:1')['text'],raw)
        finally:
            restored.close()

    @unittest.skipIf(os.name=='nt','POSIX permission bits')
    def test_new_source_database_and_private_directory_ignore_permissive_umask(self):
        previous=os.umask(0)
        try:
            database=Path(self.tmp.name)/'private-sources'/'memory.db'
            db=MemoryDB(database)
            try:
                self.assertEqual(stat.S_IMODE(database.stat().st_mode),0o600)
                self.assertEqual(stat.S_IMODE(database.parent.stat().st_mode),0o700)
            finally:
                db.close()
        finally:
            os.umask(previous)

    def test_sigterm_waits_for_active_manual_http_consolidation(self):
        entered=threading.Event()
        release=threading.Event()
        class SlowLocalModel(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                raw=json.loads(payload['messages'][1]['content'])['raw_messages']
                entered.set()
                release.wait(5)
                result={'summaries':[{'text':'活动请求中的职业记录','evidence_ids':[raw[0]['id']]}],
                        'links':[],'profiles':[]}
                body=json.dumps({'message':{'content':json.dumps(result)}}).encode()
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        model=ThreadingHTTPServer(('127.0.0.1',0),SlowLocalModel)
        model_thread=threading.Thread(target=model.serve_forever,daemon=True)
        model_thread.start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port=sock.getsockname()[1]
        database=Path(self.tmp.name)/'shutdown-memory.db'
        env={**os.environ,'GOLDFISH_LLM_PROVIDER':'ollama','GOLDFISH_LLM_MODEL':'synthetic',
             'GOLDFISH_LLM_URL':f'http://127.0.0.1:{model.server_port}'}
        process=subprocess.Popen([sys.executable,'-m','goldfish.server','--port',str(port),
            '--db',str(database)],cwd=Path(__file__).resolve().parents[1],env=env,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        def call(path,payload=None):
            body=None if payload is None else json.dumps(payload).encode()
            with urlopen(Request(f'http://127.0.0.1:{port}'+path,data=body,
                    headers={'Content-Type':'application/json'}),timeout=5) as response:
                return json.loads(response.read())
        caller=ThreadPoolExecutor(max_workers=1)
        try:
            deadline=time.monotonic()+5
            while True:
                try:
                    call('/health')
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic()>deadline:
                        self.fail('isolated HTTP backend failed to start')
                    time.sleep(.01)
            call('/ingest',{'session_id':'active','message_id':'active:1','role':'user','text':'职业测试'})
            future=caller.submit(call,'/consolidate',{'session_id':'active'})
            self.assertTrue(entered.wait(2))
            process.terminate()
            time.sleep(.05)
            self.assertIsNone(process.poll())
            release.set()
            self.assertEqual(future.result(timeout=3)['processed'],1)
            stdout,stderr=process.communicate(timeout=3)
            self.assertEqual(process.returncode,0,stderr)
        finally:
            release.set()
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=5)
            caller.shutdown(wait=True)
            model.shutdown()
            model.server_close()
        restored=MemoryDB(database)
        try:
            self.assertEqual(restored.get_unprocessed_messages('active'),[])
        finally:
            restored.close()


if __name__=='__main__': unittest.main()
