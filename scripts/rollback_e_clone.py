"""Create a new schema-5 compatible copy; original E audit/relations remain intact."""
import argparse
import os
import sqlite3
from pathlib import Path
from contextlib import closing


def clone(source, output):
    source,output=Path(source).resolve(),Path(output).resolve()
    if source==output or output.exists():raise ValueError('new_copy_path_required')
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src:
        if src.execute('PRAGMA user_version').fetchone()[0]!=6:raise ValueError('schema6_required')
        output.parent.mkdir(parents=True,exist_ok=True)
        os.close(os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
        with closing(sqlite3.connect(output)) as dest:
            src.backup(dest);dest.execute('PRAGMA foreign_keys=ON')
            with dest:
                for table in ('memory_terms','relation_history','event_relations','event_decisions','fact_evidence','event_facts','memory_events'):
                    dest.execute('DROP TABLE '+table)
                dest.execute('PRAGMA user_version=5')
            if dest.execute('PRAGMA quick_check').fetchone()[0]!='ok' or dest.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('clone_validation_failed_original_untouched')
    return output


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--db',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print('Validated schema5 copy:',clone(a.db,a.output))
