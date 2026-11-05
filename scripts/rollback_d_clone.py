"""Create a schema-4 COPY for old B2/C code without losing accepted memories.
The source is read-only. D task audit tables remain in the original v5 database.
Stop D before switching databases; do not restore an old backup over new memories.
"""
import argparse
import os
import sqlite3
from contextlib import closing
from pathlib import Path


def clone(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output or output.exists():
        raise ValueError('Use a new output path; never overwrite the source or another database')
    output.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)) as src:
        if src.execute('PRAGMA user_version').fetchone()[0] != 5:
            raise ValueError('Expected schema 5 source')
        descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(output)) as dest:
            src.backup(dest)
            dest.execute('PRAGMA foreign_keys=ON')
            with dest:
                for table in ('compact_attempts', 'compact_sources', 'compact_claims', 'compact_jobs'):
                    dest.execute('DROP TABLE '+table)
                dest.execute('PRAGMA user_version=4')
            if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok' or dest.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Rollback clone validation failed; source unchanged')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print('Validated schema-4 copy:', clone(args.db, args.output))
