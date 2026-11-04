"""Optional CPU ONNX BGE server for real evaluations, never a mock model.

Dependencies are external to Goldfish: numpy, onnxruntime, tokenizers. Download
the documented pinned model separately; this server never downloads a model.
"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time


def main():
    parser=argparse.ArgumentParser(__doc__)
    parser.add_argument('--model-dir',type=Path,required=True)
    parser.add_argument('--port',type=int,default=18321)
    parser.add_argument('--model-name',default='bge-small-zh-v1.5')
    args=parser.parse_args()
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    config=ort.SessionOptions();config.intra_op_num_threads=2;config.inter_op_num_threads=1
    session=ort.InferenceSession(str(args.model_dir/'onnx/model.onnx'),sess_options=config,providers=['CPUExecutionProvider'])
    tokenizer=Tokenizer.from_file(str(args.model_dir/'tokenizer.json'))
    tokenizer.enable_truncation(max_length=512);tokenizer.enable_padding(pad_id=0,pad_token='[PAD]')
    guard=threading.Lock()
    metadata={'model':args.model_name,'backend':'onnxruntime_cpu','pooling':'CLS','max_length':512,
        'model_sha256':hashlib.sha256((args.model_dir/'onnx/model.onnx').read_bytes()).hexdigest(),
        'tokenizer_sha256':hashlib.sha256((args.model_dir/'tokenizer.json').read_bytes()).hexdigest()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def send(self,data,status=200):
            raw=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):pass
        def do_GET(self):self.send(metadata)
        def do_POST(self):
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<2000000:raise ValueError('invalid size')
                data=json.loads(self.rfile.read(n))
                if self.path!='/v1/embeddings' or data.get('model')!=args.model_name:raise ValueError('invalid endpoint/model')
                texts=data['input']
                if not isinstance(texts,list) or not 0<len(texts)<=64 or any(not isinstance(t,str) for t in texts):raise ValueError('invalid inputs')
                started=time.perf_counter()
                with guard:
                    encoded=tokenizer.encode_batch(texts)
                    values={'input_ids':np.array([e.ids for e in encoded],dtype=np.int64),
                        'attention_mask':np.array([e.attention_mask for e in encoded],dtype=np.int64),
                        'token_type_ids':np.array([e.type_ids for e in encoded],dtype=np.int64)}
                    outputs=session.run(None,{i.name:values[i.name] for i in session.get_inputs()})
                hidden=outputs[0]
                vectors=hidden[:,0,:] if hidden.ndim==3 else hidden
                vectors=vectors/np.linalg.norm(vectors,axis=1,keepdims=True)
                count=int(sum(sum(e.attention_mask) for e in encoded))
                self.send({'object':'list','model':args.model_name,'data':[{'index':i,'embedding':v.tolist()} for i,v in enumerate(vectors)],
                    'usage':{'prompt_tokens':count,'total_tokens':count},'inference_ms':round((time.perf_counter()-started)*1000,3),
                    'truncated_input_count':sum(len(e.overflowing)>0 for e in encoded)})
            except Exception as err:self.send({'error':type(err).__name__},400)
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(json.dumps({'listening':'127.0.0.1','port':args.port,**metadata}),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()


if __name__=='__main__':main()
