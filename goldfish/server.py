"""Local Goldfish HTTP backend. No external dependencies; binds to loopback only."""
import argparse
import hmac
import json
import os
import signal
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .engine import MemoryDB
from .consolidate import consolidate_session
from .providers import ChatModel, EmbeddingModel
from .semantic import plan_recall, cover_direct_plan, deduplicate_plan
from .retrieval import candidate_pool, direct_plan

class Service:
    def __init__(self, db_path, *, retry_base=1.0):
        # Invalid model configuration must fail before opening the source DB or
        # creating worker threads.
        self.chat=ChatModel()
        self.embeddings=EmbeddingModel()
        self.db=MemoryDB(db_path)
        self.lock=self.db.lock
        self.pool=ThreadPoolExecutor(max_workers=1)
        self.pending=set()
        self._state=threading.Condition(threading.RLock())
        self._retry_due={}
        self._attempts={}
        self._retry_base=max(.01,float(retry_base))
        self._stopping=False
        self._closed=False
        self._reindex_lock=threading.Lock()
        self._query_vectors=OrderedDict()
        self._query_vector_lock=threading.Lock()
        # Reindex existing summaries when the embedding provider/model changes.
        if self.embeddings.configured:
            self.pool.submit(self._background_reindex)
        # The append-only source log is also the durable work queue: recover
        # every session whose committed cursor trails its raw messages.
        if self.chat.configured:
            with self.lock:
                sessions=[r[0] for r in self.db.db.execute('''
                    SELECT DISTINCT r.session_id FROM raw_messages r
                    LEFT JOIN consolidation_jobs j ON j.session_id=r.session_id
                    WHERE r.seq>COALESCE(j.last_seq,0) AND r.source NOT LIKE 'plugin%'
                ''')]
            for sid in sessions:
                self.schedule(sid,force=True)
        self._retry_thread=threading.Thread(target=self._retry_loop,
            name='goldfish-retry',daemon=True)
        self._retry_thread.start()

    def _reindex(self, max_batches=16):
        if not self.embeddings.configured: return {'indexed':0, 'reason':'embedding_not_configured'}
        total=0
        # Manual and background reindex requests must not issue duplicate model
        # requests for the same snapshot. No network request holds the DB lock.
        with self._reindex_lock:
            self.db.prune_vector_index()
            for _ in range(max_batches):
                with self._state:
                    if self._stopping: break
                with self.lock:
                    missing=self.db.missing_embeddings(self.embeddings.identity,limit=16)
                if missing:
                    vectors=self.embeddings.embed([s['text'] for s in missing])
                    total+=self.db.store_embeddings(self.embeddings.identity,
                        [(s['id'],v) for s,v in zip(missing,vectors)],snapshots=missing)
                else:
                    auxiliary=self.db.missing_aux_embeddings(self.embeddings.identity,self._aux_kinds(),limit=16)
                    if not auxiliary:break
                    vectors=self.embeddings.embed([s['text'] for s in auxiliary])
                    total+=self.db.store_aux_embeddings(self.embeddings.identity,vectors,auxiliary)
        return {'indexed':total}

    def _aux_kinds(self):
        return tuple(k for k in os.getenv('GOLDFISH_EMBED_AUX','profile').split(',') if k in {'profile','theme','link'})

    def _query_embedding(self,query,timeout):
        key=(self.embeddings.identity,query);now=time.monotonic()
        # One small local cache, bounded and keyed by exact model revision.
        with self._query_vector_lock:
            cached=self._query_vectors.get(key)
            if cached and now-cached[0]<300:
                self._query_vectors.move_to_end(key)
                return cached[1],True,None
            vector=self.embeddings.embed([query],timeout=timeout,purpose='query')[0]
            measurement=getattr(self.embeddings,'last_call',None)
            self._query_vectors[key]=(time.monotonic(),vector)
            while len(self._query_vectors)>128:self._query_vectors.popitem(last=False)
            return vector,False,measurement

    def _background_reindex(self):
        try:
            while not self._stopping:
                result=self._reindex()
                if not result.get('indexed'):break
        except Exception as err:
            print('[goldfish] embedding indexing unavailable:',type(err).__name__,flush=True)

    def schedule(self, sid, force=False):
        if not self.chat.configured: return False
        # Two messages normally mean a completed human/assistant turn. Force
        # also handles disposal/cancellation with a single remaining source.
        with self.lock:
            if len(self.db.get_unprocessed_messages(sid)) < (1 if force else 2):
                return False
        with self._state:
            if self._stopping or sid in self.pending: return False
            self.pending.add(sid)
            self._retry_due.pop(sid,None)
            try:
                self.pool.submit(self._drain,sid)
            except RuntimeError:
                self.pending.discard(sid)
                return False
            return True

    def _record_failure(self, sid, err):
        # Never include a model response, raw dialogue, URL or API key in logs.
        name=type(err).__name__
        with self.lock:
            with self.db.transaction():
                self.db.db.execute('''INSERT INTO consolidation_jobs(session_id,last_seq,last_error)
                    VALUES(?,0,?) ON CONFLICT(session_id) DO UPDATE SET last_error=excluded.last_error''',
                    (sid,name))
        print('[goldfish] consolidate deferred:',name,flush=True)

    def _drain(self, sid):
        failed=False
        try:
            while True:
                with self._state:
                    if self._stopping: break
                # consolidate_session owns its per-session lock and commits the
                # source cursor with all extracted rows in one transaction.
                result=consolidate_session(self.db,sid,self.chat)
                if not result.get('ok') or not result.get('processed'): break
            if self.embeddings.configured:
                try: self._reindex(max_batches=4)
                except Exception as err:
                    print('[goldfish] deferred embedding indexing:',type(err).__name__,flush=True)
        except Exception as err:
            failed=True
            self._record_failure(sid,err)
        finally:
            with self._state:
                self.pending.discard(sid)
                stopping=self._stopping
                if failed and not stopping:
                    attempt=self._attempts.get(sid,0)+1
                    self._attempts[sid]=attempt
                    delay=min(60.0,self._retry_base*2**min(attempt-1,10))
                    self._retry_due[sid]=time.monotonic()+delay
                    self._state.notify_all()
                elif not failed:
                    self._attempts.pop(sid,None)
            if not failed and not stopping:
                # Covers events appended between the last empty snapshot and
                # removal from pending; a concurrent schedule remains idempotent.
                self.schedule(sid,force=True)

    def _retry_loop(self):
        while True:
            with self._state:
                if self._stopping: return
                if not self._retry_due:
                    self._state.wait()
                    continue
                sid,due=min(self._retry_due.items(),key=lambda item:item[1])
                remaining=due-time.monotonic()
                if remaining>0:
                    self._state.wait(remaining)
                    continue
                self._retry_due.pop(sid,None)
            self.schedule(sid,force=True)

    def close(self):
        """Stop retries, finish the active transaction, then close SQLite."""
        with self._state:
            if self._closed: return
            if self._stopping:
                while not self._closed: self._state.wait()
                return
            self._stopping=True
            self._state.notify_all()
        try:
            self._retry_thread.join()
            self.pool.shutdown(wait=True,cancel_futures=True)
            self.db.close()
        finally:
            with self._state:
                self._closed=True
                self._state.notify_all()

    def handle(self, path, data):
        if not isinstance(data,dict): raise ValueError('JSON object required')
        # Network embedding calls are intentionally OUTSIDE DB locks, so an
        # Ollama cold start cannot freeze the memory event writer.
        if path=='/recall':
            started=time.perf_counter();semantic={};auxiliary={};measurements=[]
            plan=None; planning_error=None;embedding_error=None;cache_hit=False;pool=None
            depth=max(0,min(int(data.get('depth',0)),2))
            mode=os.getenv('GOLDFISH_RETRIEVAL_MODE','legacy')
            if mode not in {'legacy','embedding','hybrid','hybrid_llm'}:raise ValueError('invalid retrieval mode')
            query=str(data.get('query',''));context=str(data.get('context',''));limit=int(data.get('limit',5))
            allow_llm=self.chat.configured and os.getenv('GOLDFISH_SEMANTIC_RECALL','0')=='1'
            deadline=started+max(.1,min(float(data.get('semantic_budget_ms',10000))/1000,10))
            if self.embeddings.configured and query.strip():
                try:
                    # Keep embedding bounded alongside the optional semantic
                    # decision. Batch indexing keeps its separate 45s limit.
                    budget=max(.05,min(float(data.get('semantic_timeout_ms',1500))/1000,1.5))
                    q,cache_hit,meta=self._query_embedding(query,budget)
                    if meta:measurements.append(meta)
                    with self.lock:
                        semantic=self.db.semantic_scores(q,self.embeddings.identity)
                        auxiliary=self.db.auxiliary_scores(q,self.embeddings.identity,self._aux_kinds())
                except Exception as err:
                    # No external inference outage may block the main DSH chat.
                    print('[goldfish] semantic fallback:',type(err).__name__,flush=True)
                    embedding_error=type(err).__name__
                    if getattr(self.embeddings,'last_call',None):measurements.append(self.embeddings.last_call)
            if mode!='legacy' and (not self.embeddings.configured or embedding_error):
                mode='legacy';embedding_error=embedding_error or 'embedding_not_configured'
            if mode!='legacy':
                pool=candidate_pool(self.db,query,semantic=semantic,auxiliary=auxiliary,
                                    mode='embedding' if mode=='embedding' else 'hybrid',depth=depth,model_identity=self.embeddings.identity)
                plan=direct_plan(pool,query,context,min(3,limit))
            if allow_llm:
                try:
                    remaining=max(.1,deadline-time.perf_counter())
                    complex_query=any(s in query for s in ('为什么','为何','原因','变化','先后','去年','今年','比较','不同','哪些','之前','自称','希望','证据'))
                    exact_simple=bool(pool and pool['summaries'] and pool['summaries'][0]['channels']['exact'] and not complex_query)
                    if mode=='legacy' or mode=='hybrid_llm' and not exact_simple:
                        ranked=plan_recall(self.db,self.chat,query,context,depth=depth,timeout=remaining,limit=limit,pool=pool)
                        if ranked is not None or pool is None:plan=ranked
                    elif plan:
                        plan=cover_direct_plan(self.chat,plan,pool['summaries']+pool['profiles'],context,timeout=remaining)
                        plan=deduplicate_plan(self.db,self.chat,plan,pool['summaries']+pool['profiles'],timeout=deadline-time.perf_counter())
                    if plan and plan.get('measurement'):measurements.append(plan['measurement'])
                    if plan:measurements.extend(plan.get('extra_measurements',[]))
                except Exception as err:
                    planning_error=type(err).__name__
                    print('[goldfish] semantic recall fallback:',planning_error,flush=True)
                    if mode=='legacy':plan=None
                    if getattr(self.chat,'last_call',None):measurements.append(self.chat.last_call)
            with self.lock:
                result={'ok':True,**self.db.recall(
                    str(data.get('query','')),str(data.get('context','')),
                    max(0,min(int(data.get('limit',5)),12)),
                    max(250,min(int(data.get('max_chars',1800)),8000)),
                    depth,semantic_scores=semantic,semantic_plan=plan)}
                result['semantic_fallback']=planning_error
                if plan: result['semantic_plan']={k:v for k,v in plan.items() if k not in {'payload','raw_output'}}
                result['retrieval']={'mode':mode,'embedding_error':embedding_error,'query_embedding_cache_hit':cache_hit,
                    'measurements':measurements,'latency_ms':round((time.perf_counter()-started)*1000,3),
                    'candidate_count':pool['candidate_count'] if pool else None,'active_count':pool['active_count'] if pool else None,
                    'channels':pool['channels'] if pool else None}
                return result
        if path=='/reindex':
            if data.get('force'):
                with self._reindex_lock:self.db.prune_vector_index(force=True)
                with self._query_vector_lock:self._query_vectors.clear()
            return {'ok':True,**self._reindex()}
        if path=='/consolidate':
            sid=str(data['session_id'])
            try:
                result=consolidate_session(self.db,sid,self.chat)
            except Exception as err:
                self._record_failure(sid,err)
                self.schedule(sid,force=True)
                raise
            if result.get('ok') and self.embeddings.configured:
                try: result['embedding']=self._reindex(max_batches=4)
                except Exception as err: result['embedding']={'error':type(err).__name__}
            return result
        with self.lock:
            if path=='/health': return {'ok':True,'version':'0.3.0'}
            if path=='/providers': return {'ok':True,'chat':self.chat.cfg.public(),
                                         'embedding':self.embeddings.cfg.public()}
            if path=='/ingest':
                for key in ('session_id','role','text'):
                    if not isinstance(data.get(key),str) or not data[key].strip():
                        raise ValueError('nonempty message strings required')
                for key in ('message_id','event_time','source'):
                    if key in data and (not isinstance(data[key],str) or not data[key]):
                        raise ValueError('invalid message metadata')
                mid=self.db.add_message(
                    data['session_id'],data['role'],data['text'],
                    data.get('event_time'),data.get('message_id'),data.get('source','human'))
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
                counts={k:self.db.db.execute('SELECT COUNT(*) FROM '+k).fetchone()[0] for k in names}
                with self._state:
                    return {'ok':True,**counts,'pending_sessions':len(self.pending),
                            'retry_sessions':len(self._retry_due)}
        raise ValueError('unknown API route')

def serve(host,port,db_path):
    service=Service(db_path)
    token=os.getenv('GOLDFISH_LOCAL_TOKEN','')
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)
        def log_message(self,*args): pass  # Do not log private queries.
        def send(self,data,status=200):
            body=json.dumps(data,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            try:
                self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):
                pass  # The DSH caller may already have cancelled its recall.
        def dispatch(self):
            if token and not hmac.compare_digest(self.headers.get('X-Goldfish-Token',''),token):
                return self.send({'ok':False,'error':'unauthorized'},401)
            try:
                if self.path not in {'/health','/ingest','/recall','/schedule','/consolidate','/evidence','/link_evidence','/stats','/providers','/reindex'}:
                    return self.send({'ok':False,'error':'not_found'},404)
                data={}
                if self.command=='POST':
                    n=int(self.headers.get('Content-Length','0'))
                    if n<0: return self.send({'ok':False,'error':'invalid_length'},400)
                    if n>2000000: return self.send({'ok':False,'error':'body_too_large'},413)
                    data=json.loads(self.rfile.read(n) or b'{}')
                result=service.handle(self.path,data)
                self.send(result)
            except (ValueError,KeyError,TypeError) as err:
                self.send({'ok':False,'error':type(err).__name__},400)
            except Exception as err:
                self.send({'ok':False,'error':type(err).__name__},500)
        def do_GET(self): self.dispatch()
        def do_POST(self): self.dispatch()
    try:
        httpd=ThreadingHTTPServer((host,port),Handler)
        # ThreadingHTTPServer defaults to daemon request threads. A manual
        # consolidate/recall may still use SQLite after the listener stops.
        httpd.daemon_threads=False
    except Exception:
        service.close()
        raise
    print(f'[goldfish] listening on {host}:{port}, database {db_path}',flush=True)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        httpd.server_close();service.close()

def main():
    parser=argparse.ArgumentParser(description='Goldfish local long-term memory service')
    parser.add_argument('--port',type=int,default=18320)
    parser.add_argument('--db',default=str(Path.home()/'.goldfish-memory'/'memory.sqlite3'))
    args=parser.parse_args()
    def terminate(*_): raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,terminate)
    serve('127.0.0.1',args.port,args.db)

if __name__=='__main__': main()
