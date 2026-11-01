"""Transactional, checksum-verified SQL migrations. No database client at import."""
import argparse
from hashlib import sha256
from functools import lru_cache
import os
from pathlib import Path
import re

import psycopg
from psycopg.pq import TransactionStatus

MIGRATIONS = Path(__file__).resolve().parents[2] / 'migrations'


def pooled_connection(conninfo):
    """Transaction-pooler compatible runtime connection; never prepare statements."""
    try:
        return psycopg.connect(conninfo, autocommit=True, connect_timeout=5, prepare_threshold=None)
    except psycopg.Error:
        raise OSError('Runtime database connection unavailable') from None


def direct_connection(conninfo):
    """Migrations/checkpoint setup need a session-preserving direct endpoint."""
    from psycopg.conninfo import conninfo_to_dict
    try:
        host = conninfo_to_dict(conninfo).get('host', '')
        if '-pooler' in host:
            raise OSError('Migrations require the direct endpoint, not a pooler')
        return psycopg.connect(conninfo, autocommit=True, connect_timeout=5)
    except psycopg.Error:
        raise OSError('Direct database connection unavailable') from None


def runtime_database_url():
    """Resolve the app URL privately; AWS stores only a parameter name in env."""
    if os.environ.get('LEDGERGUARD_ENV', 'local') == 'local':
        value = os.environ.get('APP_DATABASE_URL')
        if not value:
            raise OSError('Review database is not configured')
        return value
    name = os.environ.get('APP_DATABASE_PARAMETER')
    if not name:
        raise OSError('Review database parameter is not configured')
    value = parameter_value(name)
    from psycopg.conninfo import conninfo_to_dict
    try:
        tls = conninfo_to_dict(value).get('sslmode')
    except (psycopg.Error, TypeError, ValueError):
        raise OSError('Invalid runtime database configuration') from None
    if tls not in {'require', 'verify-ca', 'verify-full'}:
        raise OSError('AWS database connections require TLS')
    return value


def parameter_value(name):
    # Cache for a warm Lambda container. Restart/redeploy after secret rotation.
    return _parameter_value(name)


@lru_cache(maxsize=4)
def _parameter_value(name):
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        parameter = boto3.client('ssm').get_parameter(Name=name, WithDecryption=True)['Parameter']
        if parameter.get('Type') != 'SecureString' or not parameter.get('Value'):
            raise OSError('Database parameter is not a configured SecureString')
        return parameter['Value']
    except (BotoCoreError, ClientError, KeyError, TypeError):
        raise OSError('Database parameter unavailable') from None


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
        with direct_connection(url) as conn:
            applied = migrate(conn)
            setup_checkpoints(conn)
    except (psycopg.Error, OSError):
        # Connection exceptions can include credential-bearing connection strings.
        parser.exit(1, 'Database migration failed; check owner connection and server configuration.\n')
    print('Applied migrations: ' + (', '.join(applied) or 'none'))


if __name__ == '__main__':
    main()
