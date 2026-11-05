"""Build a deterministic, screened source archive from exactly one clean Git commit."""
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

root=Path(__file__).resolve().parents[1]
def git(*args):return subprocess.check_output(['git',*args],cwd=root)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if subprocess.run(['git','diff','--quiet','HEAD','--'],cwd=root).returncode:
        raise SystemExit('Commit reviewed source changes before packaging.')
    commit=git('rev-parse','HEAD').decode().strip()
    paths=git('ls-tree','-r','--name-only','-z','HEAD').decode().split('\0')
    selected=[]
    for name in filter(None,paths):
        path=Path(name)
        if any(p in {'backups','node_modules','__pycache__','artifacts','data','sessions'} for p in path.parts) or name.startswith(('evals/results/','docs/upgrade-')):
            raise SystemExit('Private/runtime path tracked: '+name)
        if path.suffix.lower() in {'.db','.sqlite','.sqlite3','.xml','.key','.pem','.log','.gz','.zstd'} or path.name in {'installation.json','processes.json','.credentials.yaml'} or path.name.startswith('.env'):
            raise SystemExit('Runtime/secret artifact tracked: '+name)
        data=git('show','HEAD:'+name)
        if b'SQLite format 3\0' in data or re.search(rb'\b(?:sk-[A-Za-z0-9_-]{22,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{35,})\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',data):
            raise SystemExit('Possible credential/database in source: '+name)
        if path.suffix.lower()=='.cmd':data=data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
        selected.append((name,data))
    manifest={'version':'0.8.0','schema':6,'commit':commit,'lineEndings':'Windows .cmd files use CRLF; other files are Git blob bytes',
        'files':{name:hashlib.sha256(data).hexdigest() for name,data in selected}}
    selected.append(('SOURCE_MANIFEST.json',(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode()))
    output=args.output.resolve();output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists():raise SystemExit('Use a new output path; existing artifacts are not overwritten.')
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,data in selected:
            info=zipfile.ZipInfo('Goldfish-Memory-v0.8.0/'+name,date_time=(2026,11,5,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED;info.external_attr=0o100644<<16
            archive.writestr(info,data)
    checksum=hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix+'.sha256').write_text(checksum+'  '+output.name+'\n',encoding='ascii')
    print(json.dumps({'version':'0.8.0','commit':commit,'fileCount':len(selected),'sha256':checksum}))


if __name__=='__main__':main()
