"""Back up with SQLite backup API, then apply Goldfish additive migrations."""
import argparse,sqlite3
from pathlib import Path
from datetime import datetime,timezone
from goldfish.engine import MemoryDB
p=argparse.ArgumentParser();p.add_argument('--db',type=Path,required=True);a=p.parse_args()
if a.db.exists():
    backup=a.db.with_name(a.db.name+'.backup-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    with sqlite3.connect(a.db) as source,sqlite3.connect(backup) as dest:source.backup(dest)
    print('Local backup created:',backup)
db=MemoryDB(a.db)
try:print('Schema ready:',db.db.execute('PRAGMA user_version').fetchone()[0])
finally:db.close()
