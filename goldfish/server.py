"""Local Goldfish HTTP backend. No external dependencies; binds to loopback only."""
import argparse
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .engine import MemoryDB
from .consolidate import consolidate_session
from .providers import ChatModel, EmbeddingModel

class Service:
    def __init__(self, db_path):
        self.db=MemoryDB(db_path)
        self.lock=threading.RLock()
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.pending=set()
        self.chat=ChatModel()
        self.embeddings=EmbeddingModel()
        # Reindex existing summaries when the embedding provider/model changes.
        if self.embeddings.configured:
            self.pool.submit(self._background_reindex)

    def _reindex(self, max_batches=16):
        if not self.embeddings.configured: return {'indexed':0, 'reason':'embedding_not_configured'}
        total=0
        for _ in range(max_batches):
            with self.lock:
                missing=self.db.missing_embeddings(self.embeddings.identity,limit=16)
            if not missing: break
            vectors=self.embeddings.embed([s['text'] for s in missing])
            with self.lock:
                self.db.store_embeddings(self.embeddings.identity,
                    [(s['id'],v) for s,v in zip(missing,vectors)])
            total+=len(missing)
        return {'indexed':total}

    def _background_reindex(self):
        try:
            self._reindex()
        except Exception as err:
            print('[goldfish] embedding indexing unavailable:',type(err).__name__,flush=True)

    def schedule(self, sid, force=False):
        if sid in self.pending or not self.chat.configured: return False
        if not force and len(self.db.get_unprocessed_messages(sid)) < 6: return False
        self.pending.add(sid)
        def job():
            try:
                with self.lock:
                    consolidate_session(self.db,sid,self.chat)
                if self.embeddings.configured:
                    try: self._reindex(max_batches=4)
                    except Exception as err:
                        print('[goldfish] deferred embedding indexing:',type(err).__name__,flush=True)
            except Exception as err:
                # Retain raw source and cursor to permit safe manual retry.
                print('[goldfish] consolidate error:',str(err)[:160],flush=True)
            finally: self.pending.discard(sid)
        self.pool.submit(job)
        return True

    def handle(self, path, data):
        # Network embedding calls are intentionally OUTSIDE DB locks, so an
        # Ollama cold start cannot freeze the memory event writer.
        if path=='/recall':
            semantic={}
            if self.embeddings.configured:
                try:
                    q=self.embeddings.embed([str(data.get('query',''))])[0]
                    with self.lock:
                        semantic=self.db.semantic_scores(q,self.embeddings.identity)
                except Exception as err:
                    # No external inference outage may block the main DSH chat.
                    print('[goldfish] semantic fallback:',type(err).__name__,flush=True)
            with self.lock:
                return {'ok':True,**self.db.recall(
                    str(data.get('query','')),str(data.get('context',''))[:70000],
                    max(0,min(int(data.get('limit',5)),12)),
                    max(250,min(int(data.get('max_chars',1800)),8000)),
                    max(0,min(int(data.get('depth',2)),2)),semantic_scores=semantic)}
        if path=='/reindex':
            return {'ok':True,**self._reindex()}
        if path=='/consolidate':
            with self.lock:
                result=consolidate_session(self.db,str(data['session_id']),self.chat)
            if result.get('ok') and self.embeddings.configured:
                try: result['embedding']=self._reindex(max_batches=4)
                except Exception as err: result['embedding']={'error':type(err).__name__}
            return result
        with self.lock:
            if path=='/health': return {'ok':True,'version':'0.2.0'}
            if path=='/providers': return {'ok':True,'chat':self.chat.cfg.public(),
                                         'embedding':self.embeddings.cfg.public()}
            if path=='/ingest':
                if not isinstance(data,dict): raise ValueError('JSON object required')
                mid=self.db.add_message(
                    str(data['session_id']),str(data['role']),str(data['text']),
                    data.get('event_time'),data.get('message_id'),str(data.get('source','human')))
                if mid and data.get('consolidate_after',False): self.schedule(str(data['session_id']))
                return {'ok':True,'message_id':mid}
            if path=='/schedule':
                return {'ok':True,'scheduled':self.schedule(str(data['session_id']),force=True)}
            if path=='/evidence':
                sid=str(data['summary_id'])
                return {'ok':True,'sources':self.db.evidence_for(sid)}
            if path=='/link_evidence':
                return {'ok':True,'sources':self.db.link_sources(str(data['link_id']))}
            if path=='/stats':
                names=['raw_messages','summaries','themes','links','profile']
                return {'ok':True,**{k:self.db.db.execute('SELECT COUNT(*) FROM '+k).fetchone()[0] for k in names}}
        raise ValueError('unknown API route')

def serve(host,port,db_path):
    service=Service(db_path)
    token=os.getenv('GOLDFISH_LOCAL_TOKEN','')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass  # Do not log private queries.
        def send(self,data,status=200):
            body=json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
        def dispatch(self):
            if token and self.headers.get('X-Goldfish-Token','')!=token:
                return self.send({'ok':False,'error':'unauthorized'},401)
            try:
                if self.path not in {'/health','/ingest','/recall','/schedule','/consolidate','/evidence','/link_evidence','/stats','/providers','/reindex'}:
                    return self.send({'ok':False,'error':'not_found'},404)
                data={}
                if self.command=='POST':
                    n=int(self.headers.get('Content-Length','0'))
                    if n>200000: return self.send({'ok':False,'error':'body_too_large'},413)
                    data=json.loads(self.rfile.read(n) or b'{}')
                result=service.handle(self.path,data)
                self.send(result)
            except (ValueError,KeyError,TypeError) as err:
                self.send({'ok':False,'error':str(err)[:200]},400)
            except Exception as err:
                self.send({'ok':False,'error':type(err).__name__},500)
        def do_GET(self): self.dispatch()
        def do_POST(self): self.dispatch()
    httpd=ThreadingHTTPServer((host,port),Handler)
    print(f'[goldfish] listening on {host}:{port}, database {db_path}',flush=True)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        httpd.server_close();service.pool.shutdown(wait=False);service.db.close()

def main():
    parser=argparse.ArgumentParser(description='Goldfish local long-term memory service')
    parser.add_argument('--port',type=int,default=18320)
    parser.add_argument('--db',default=str(Path.home()/'.goldfish-memory'/'memory.sqlite3'))
    args=parser.parse_args()
    serve('127.0.0.1',args.port,args.db)

if __name__=='__main__': main()
