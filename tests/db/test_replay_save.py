"""A genuine recorded replay persists its full findings under the app role."""
from decimal import Decimal
from pathlib import Path

import boto3
import pytest

from ledgerguard import pipeline_eval
from ledgerguard.gateway import MODEL_ID, NOVA_MODEL_ID
from ledgerguard.recording import ReplayClient


def test_replay_save_persists_findings_and_provenance(pipeline_case, app_conninfo, db_owner, tmp_path, monkeypatch):
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
