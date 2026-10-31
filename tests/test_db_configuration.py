"""The real-database suite must never silently skip when running in CI."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('ci,exit_code,expected', [('', 0, 'skipped'), ('true', 1, 'DATABASE_URL is required in CI')])
def test_database_url_skip_or_fail(ci, exit_code, expected):
    environment = dict(os.environ)
    environment.pop('DATABASE_URL', None)
    environment['CI'] = ci
    completed = subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', 'tests/db/test_db.py', '-k', 'test_no_binary_pdf_columns'],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == exit_code
    assert expected in completed.stdout
