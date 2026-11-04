"""Package committed project only; never walk user data or temporary workspace."""
import argparse,hashlib,subprocess,zipfile
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
root=Path(__file__).resolve().parents[1]
files=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
import re
selected=[]
for name in filter(None,files):
 path=root/name
 if path.suffix.lower() in {'.db','.sqlite','.sqlite3','.xml'} or '.env' in path.name or any(x in path.parts for x in ('node_modules','__pycache__','artifacts')):raise SystemExit('Unexpected private/runtime file: '+name)
 data=path.read_bytes()
 if path.suffix.lower()=='.cmd':data=data.replace(b'\r\n',b'\n').replace(b'\n',b'\r\n')
 if re.search(rb'sk-[A-Za-z0-9]{25,}',data):raise SystemExit('Possible credential in tracked project: '+name)
 selected.append((name,data))
a.output.parent.mkdir(parents=True,exist_ok=True)
with zipfile.ZipFile(a.output,'w',zipfile.ZIP_DEFLATED) as z:
 for name,data in selected:z.writestr('GoldfishMemory/'+name,data)
print('Files:',len(selected));print('SHA256:',hashlib.sha256(a.output.read_bytes()).hexdigest())
