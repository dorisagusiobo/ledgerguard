"""Independent audit destinations; failures stop work with OSError."""
from contextlib import contextmanager
import json
from pathlib import Path
from typing import ContextManager, Protocol

import psycopg


class AuditWriter(Protocol):
    def append(self, record: dict) -> None: ...
    def authorize(self, record: dict) -> None: ...


class AuditSink(Protocol):
    def session(self) -> ContextManager[AuditWriter]: ...


class JsonlSink:
    """Keep the existing one-final-record-per-model-attempt JSONL format."""
    def __init__(self, path):
        self.path = Path(path).resolve()
        project_data = Path(__file__).resolve().parents[2] / 'data'
        if self.path.is_relative_to(project_data) or 'data' in self.path.parts:
            raise ValueError('Audit log must be outside data directories')
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def session(self):
        # Holding an open writable file preserves the existing pre-call check.
        with self.path.open('a', encoding='utf-8') as log:
            class Writer:
                def append(self, record):
                    log.write(json.dumps(record, sort_keys=True) + '\n')
                    log.flush()

                def authorize(self, record):
                    # The open writable file is the existing JSONL pre-call check.
                    pass
            yield Writer()


def app_connection(conninfo):
    """Runtime DB sessions must operate as ledgerguard_app, never the owner."""
    try:
        conn = psycopg.connect(conninfo, autocommit=True, connect_timeout=5)
        try:
            if conn.execute('SELECT current_user').fetchone()[0] != 'ledgerguard_app':
                raise OSError('Runtime database connection must use ledgerguard_app')
        except BaseException:
            conn.close()
            raise
        return conn
    except psycopg.Error:
        raise OSError('Runtime database connection unavailable') from None


class PostgresSink:
    """Each append commits independently; never enlist in a caller transaction."""
    def __init__(self, conninfo):
        self._conninfo = conninfo

    @contextmanager
    def session(self):
        from ledgerguard.store import append_audit

        try:
            with app_connection(self._conninfo) as conn:
                class Writer:
                    def append(self, record):
                        append_audit(conn, record)

                    def authorize(self, record):
                        # Durable authorization precedes every model invocation.
                        self.append({**record, 'phase': 'authorization', 'outcome': 'authorized'})
                yield Writer()
        except psycopg.Error:
            # Provider/database exceptions can contain configuration or data.
            raise OSError('Postgres audit write failed') from None
