"""Authenticated operator retry of one retained immutable compaction range."""
import argparse
import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--port',type=int,default=18320)
    args=parser.parse_args()
    key=(Path(os.environ['LOCALAPPDATA'])/'GoldfishMemory/compaction-write.key').read_bytes()
    if len(key)!=32:raise ValueError('local_compaction_capability_unavailable')
    raw=json.dumps({'action':'retry','job_id':args.job_id,'issued_at':time.time()},ensure_ascii=False).encode()
    request=Request(f'http://127.0.0.1:{args.port}/compaction',raw,
        {'Content-Type':'application/json','X-Goldfish-Compaction-Signature':hmac.new(key,raw,hashlib.sha256).hexdigest()})
    with urlopen(request,timeout=10) as response:
        result=json.load(response)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
