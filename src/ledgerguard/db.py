"""Transactional, checksum-verified SQL migrations. No database client at import."""
import argparse
from hashlib import sha256
import os
from pathlib import Path
import re

import psycopg
from psycopg.pq import TransactionStatus

MIGRATIONS = Path(__file__).resolve().parents[2] / 'migrations'


def migrate(conn, path: Path = MIGRATIONS) -> list[str]:
    """Apply pending files atomically; refuse missing/changed applied migrations.

    Requires an idle connection so success means committed, not a nested savepoint.
    The database advisory lock serializes concurrent migration runners.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('Migrations require an idle connection')
    files = sorted(Path(path).glob('*.sql'))
    if not files or any(not re.fullmatch(r'\d{3}_[a-z0-9_]+\.sql', f.name) for f in files):
        raise ValueError('Expected numbered SQL migration files')
    if len({f.name[:3] for f in files}) != len(files):
        raise ValueError('Migration numbers must be unique')
    contents = {f.name: f.read_bytes() for f in files}
    applied_now = []
    with conn.transaction():
        conn.execute('SELECT pg_advisory_xact_lock(17492103)')
        conn.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
        applied = dict(conn.execute('SELECT name, sha256 FROM schema_migrations').fetchall())
        for name, checksum in applied.items():
            if name not in contents or sha256(contents[name]).hexdigest() != checksum:
                raise ValueError(f'Applied migration missing or changed: {name}')
        for name, content in contents.items():
            if name in applied:
                continue
            if applied and name < max(applied):
                raise ValueError(f'Migration inserted before applied history: {name}')
            conn.execute(content.decode('utf-8'))
            conn.execute('INSERT INTO schema_migrations (name, sha256) VALUES (%s, %s)',
                         (name, sha256(content).hexdigest()))
            applied_now.append(name)
    return applied_now


CHECKPOINT_TABLES = ('checkpoints', 'checkpoint_blobs', 'checkpoint_writes')


def setup_checkpoints(conn):
    """Owner-only, autocommitted LangGraph setup; runtime never calls setup."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row, tuple_row

    if not conn.autocommit or conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('Checkpoint setup requires an idle autocommit connection')
    with conn.cursor(row_factory=tuple_row) as cursor:
        row = cursor.execute("SELECT current_user, tableowner FROM pg_tables WHERE schemaname='public' AND tablename='runs'").fetchone()
        if row is None or row[0] != row[1] or row[0] == 'ledgerguard_app':
            raise PermissionError('Checkpoint setup requires the migration owner')
    original_factory = conn.row_factory
    conn.execute('SELECT pg_advisory_lock(17492104)')
    try:
        conn.row_factory = dict_row
        PostgresSaver(conn).setup()
        conn.execute('REVOKE ALL ON checkpoint_migrations FROM PUBLIC, ledgerguard_app')
        for table in CHECKPOINT_TABLES:
            # Fixed identifiers, never user input.
            conn.execute(f'REVOKE ALL ON {table} FROM PUBLIC, ledgerguard_app')
            conn.execute(f'GRANT SELECT, INSERT, UPDATE ON {table} TO ledgerguard_app')
    finally:
        conn.row_factory = original_factory
        conn.execute('SELECT pg_advisory_unlock(17492104)')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['migrate'])
    parser.parse_args(argv)
    url = os.environ.get('DATABASE_URL')
    if not url:
        parser.error('DATABASE_URL is required; use the owner connection')
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            applied = migrate(conn)
            setup_checkpoints(conn)
    except psycopg.Error:
        # Connection exceptions can include credential-bearing connection strings.
        parser.exit(1, 'Database migration failed; check owner connection and server configuration.\n')
    print('Applied migrations: ' + (', '.join(applied) or 'none'))


if __name__ == '__main__':
    main()
