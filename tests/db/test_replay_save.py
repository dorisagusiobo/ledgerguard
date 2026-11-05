"""A genuine recorded replay persists its full findings under the app role."""
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import boto3
import pytest

from ledgerguard import pipeline_eval
from ledgerguard.gateway import MODEL_ID, NOVA_MODEL_ID
from ledgerguard.recording import ReplayClient
from ledgerguard.store import append_audit


def test_replay_save_persists_findings_and_provenance(pipeline_case, app_conninfo, db_owner, db_app, tmp_path, monkeypatch):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'local')
    monkeypatch.setenv('APP_DATABASE_URL', app_conninfo)
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('No AWS calls during replay'))
    recording = Path(__file__).resolve().parents[2] / 'evals/recordings/pipeline-dev.jsonl'
    client = ReplayClient(recording, data=pipeline_case[0], model_ids={
        'extract': NOVA_MODEL_ID, 'policy': NOVA_MODEL_ID, 'investigate': MODEL_ID})
    report = pipeline_eval.compare(pipeline_case[0], client=client, max_cost=Decimal('0.50'),
                                   audit_path=tmp_path / 'replay-audit.jsonl', save=True)
    run_id = report['pipeline_run_id']
    assert db_owner.execute('SELECT mode, git_revision FROM runs WHERE run_id=%s', (run_id,)).fetchone() == (
        'replay', client.header['git_commit'])
    assert db_owner.execute('SELECT count(*) FROM findings WHERE run_id=%s', (run_id,)).fetchone() == (14,)
    assert db_owner.execute('SELECT count(*) FROM documents WHERE run_id=%s', (run_id,)).fetchone() == (38,)
    assert db_owner.execute('SELECT count(*) FROM audit_log WHERE pipeline_run_id=%s', (run_id,)).fetchone()[0] > 0
    # Exercise imported audit provenance even when replay generates fresh gateway IDs.
    original = db_owner.execute(
        'SELECT time, model, decision, outcome, cost FROM audit_log WHERE pipeline_run_id=%s LIMIT 1',
        (run_id,)).fetchone()
    record = dict(zip(('time', 'model', 'decision', 'outcome', 'cost'), original))
    record['run_id'] = uuid4().hex + ':INV-1'
    append_audit(db_app, record, pipeline_run_id=run_id)
    rows = db_owner.execute(
        'SELECT run_id, pipeline_run_id FROM run_audit WHERE resolved_run_id=%s',
        (run_id,)).fetchall()
    assert len(rows) == db_owner.execute(
        'SELECT count(*) FROM audit_log WHERE pipeline_run_id=%s', (run_id,)).fetchone()[0]
    assert rows and all(linked_run == run_id for _, linked_run in rows)
    # The explicit link must win over an imported recording's different prefix.
    assert any(recorded_id.split(':', 1)[0] != run_id for recorded_id, _ in rows)
