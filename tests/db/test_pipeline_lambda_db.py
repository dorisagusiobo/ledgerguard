"""Production runner wiring exercised against isolated Postgres with fake models."""
from decimal import Decimal
from hashlib import sha256

import pytest

from fakes import FakeClient
from ledgerguard import pipeline_lambda as module
from ledgerguard.rules import load_reference


@pytest.fixture
def runtime(pipeline_case, app_conninfo, monkeypatch):
    root, pdfs, invoices, answers, _ = pipeline_case
    selected = {name: pdfs[name] for name in sorted(pdfs)[:4]}
    hashes = {name: sha256(pdf).hexdigest() for name, pdf in selected.items()}
    client = FakeClient(pdfs, invoices, answers, tool=True)
    monkeypatch.setattr(module, 'runtime_database_url', lambda: app_conninfo)
    monkeypatch.setattr(module, 'bedrock_client', lambda: client)
    monkeypatch.setattr(module, 'build_revision', lambda: 'a' * 40 + '-dirty')
    # Runtime is forbidden from setting up schema/checkpoints, even in tests.
    monkeypatch.setattr(module.PostgresSaver, 'setup', lambda *a, **kw: pytest.fail('No runtime setup'))
    return selected, hashes, load_reference(root), client


def test_live_run_saved_with_app_role_and_provenance(runtime, db_owner):
    pdfs, hashes, reference, client = runtime
    result = module.execute_batch(pdfs, hashes, reference, Decimal('0.50'))
    assert result['status'] == 'completed' and result['finding_count'] == 2
    assert result['document_count'] == 4 and len(client.calls) > 0
    run_id = result['run_id']
    assert db_owner.execute('SELECT mode, git_revision FROM runs WHERE run_id=%s', (run_id,)).fetchone() == (
        'live', 'a' * 40 + '-dirty')
    assert dict(db_owner.execute('SELECT document_id, pdf_sha256 FROM documents WHERE run_id=%s', (run_id,))) == hashes
    assert db_owner.execute('SELECT count(*) FROM findings WHERE run_id=%s', (run_id,)).fetchone() == (2,)
    assert db_owner.execute('SELECT count(*) FROM audit_log WHERE run_id LIKE %s', (run_id + ':%',)).fetchone()[0] > 0
    rows = db_owner.execute(
        'SELECT document_id, cost, pipeline_run_id FROM run_audit WHERE resolved_run_id=%s',
        (run_id,)).fetchall()
    assert len(rows) == db_owner.execute(
        'SELECT count(*) FROM audit_log WHERE run_id LIKE %s', (run_id + ':%',)).fetchone()[0]
    assert all(document_id in pdfs and linked_run is None for document_id, _, linked_run in rows)
    assert sum((cost or Decimal('0') for _, cost, _ in rows), Decimal('0')) == Decimal(result['batch_cost'])
    assert db_owner.execute('SELECT count(*) FROM checkpoints WHERE thread_id LIKE %s', (run_id + ':%',)).fetchone()[0] > 0


def test_batch_block_is_saved_as_stopped_without_model_calls(runtime, db_owner):
    pdfs, hashes, reference, client = runtime
    result = module.execute_batch(pdfs, hashes, reference, Decimal('0'))
    assert result['status'] == 'stopped' and result['stopped_reason'] == 'max_cost_exceeded'
    assert result['batch_cost'] == '0' and not client.calls
    assert db_owner.execute('SELECT mode, status, stopped_reason FROM runs WHERE run_id=%s', (result['run_id'],)).fetchone() == (
        'live', 'stopped', 'max_cost_exceeded')
    assert db_owner.execute('SELECT count(*) FROM documents WHERE run_id=%s', (result['run_id'],)).fetchone() == (4,)
    assert db_owner.execute(
        'SELECT COALESCE(sum(cost), 0) FROM run_audit WHERE resolved_run_id=%s',
        (result['run_id'],)).fetchone() == (Decimal('0'),)


def test_owner_role_refused_before_model_or_save(runtime, postgres_database, monkeypatch, db_owner):
    pdfs, hashes, reference, client = runtime
    monkeypatch.setattr(module, 'runtime_database_url', lambda: postgres_database)
    before = db_owner.execute('SELECT count(*) FROM runs').fetchone()
    with pytest.raises(OSError, match='ledgerguard_app'):
        module.execute_batch(pdfs, hashes, reference, Decimal('0.50'))
    assert not client.calls
    assert db_owner.execute('SELECT count(*) FROM runs').fetchone() == before
