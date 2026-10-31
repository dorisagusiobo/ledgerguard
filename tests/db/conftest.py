"""Isolated real-Postgres tests: a new database per pytest session, never public data."""
import os
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from ledgerguard.db import migrate


def database_url():
    url = os.environ.get('DATABASE_URL')
    if not url:
        if os.environ.get('CI', '').lower() == 'true':
            pytest.fail('DATABASE_URL is required in CI; database tests must run', pytrace=False)
        pytest.skip('DATABASE_URL not set; local Postgres tests skipped')
    return url


@pytest.fixture(scope='session')
def postgres_database():
    # Only the configuration name is reported. Never print connection strings.
    url = database_url()
    name = 'ledgerguard_test_' + uuid4().hex
    try:
        admin = psycopg.connect(url, autocommit=True)
    except psycopg.Error:
        pytest.fail('Cannot connect to test Postgres; check DATABASE_URL', pytrace=False)
    try:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        test_url = make_conninfo(url, dbname=name)
        with psycopg.connect(test_url, autocommit=True) as conn:
            migrate(conn)
        yield test_url
    finally:
        admin.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def db_owner(postgres_database):
    with psycopg.connect(postgres_database, autocommit=True) as conn:
        yield conn


@pytest.fixture
def db_app(postgres_database):
    with psycopg.connect(postgres_database, autocommit=True) as conn:
        # This tests the actual role's privileges without altering the shared
        # cluster role/password. The owner login is never used by store calls.
        conn.execute('SET ROLE ledgerguard_app')
        assert conn.execute('SELECT current_user').fetchone()[0] == 'ledgerguard_app'
        yield conn
