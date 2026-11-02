"""Database-enforced privileges, immutable migration history and atomic storage."""
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from fakes import FakeClient
from ledgerguard.db import MIGRATIONS, migrate
from ledgerguard.gateway import Budget
from ledgerguard.pipeline import PipelineModels, run_pipeline
from ledgerguard.rules import load_reference
from ledgerguard.store import RunMetadata, append_audit, load_findings, save_run


def test_migrations_idempotent_and_checksum_protected(db_owner, tmp_path):
    assert migrate(db_owner) == []
    copytree(MIGRATIONS, tmp_path / 'migrations')
    initial = tmp_path / 'migrations' / '001_initial.sql'
    initial.write_text(initial.read_text() + '\n-- changed\n')
    with pytest.raises(ValueError, match='Applied migration missing or changed'):
        migrate(db_owner, tmp_path / 'migrations')
    assert migrate(db_owner) == []
    initial.unlink()
    # Keep one pending file so the missing-applied-file check is exercised.
    (initial.parent / '006_pending.sql').write_text('SELECT 1;')
    with pytest.raises(ValueError, match='missing or changed'):
        migrate(db_owner, initial.parent)


def test_pending_migration_ddl_and_history_roll_back(db_owner, tmp_path):
    copytree(MIGRATIONS, tmp_path / 'migrations')
    (tmp_path / 'migrations/006_broken.sql').write_text(
        'CREATE TABLE should_rollback (id INTEGER); SELECT definitely_missing_column;')
    with pytest.raises(psycopg.Error):
        migrate(db_owner, tmp_path / 'migrations')
    assert db_owner.execute("SELECT to_regclass('should_rollback')").fetchone()[0] is None
    assert db_owner.execute("SELECT count(*) FROM schema_migrations WHERE name='006_broken.sql'").fetchone()[0] == 0


def audit_record():
    return {'time': datetime.now(timezone.utc), 'run_id': 'synthetic-' + uuid4().hex,
            'model': 'synthetic-model', 'decision': 'allow', 'outcome': 'success',
            'reason': None, 'input_tokens': 100, 'output_tokens': 20,
            'cost': Decimal('0.0006'), 'reserved_cost': Decimal('0.001')}


def test_app_audit_insert_select_only(db_owner, db_app):
    record = audit_record()
    append_audit(db_app, record)
    row = db_app.execute('SELECT cost FROM audit_log WHERE run_id=%s', (record['run_id'],)).fetchone()
    assert row == (Decimal('0.0006'),)
    assert isinstance(row[0], Decimal)
    for command in ['UPDATE audit_log SET outcome=\'changed\' WHERE run_id=%s',
                    'DELETE FROM audit_log WHERE run_id=%s']:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_app.execute(command, (record['run_id'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_app.execute('TRUNCATE audit_log')
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'):
        db_owner.execute('UPDATE audit_log SET outcome=\'changed\' WHERE run_id=%s', (record['run_id'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'):
        db_owner.execute('DELETE FROM audit_log WHERE run_id=%s', (record['run_id'],))
    with pytest.raises(psycopg.errors.InsufficientPrivilege, match='append-only'):
        db_owner.execute('TRUNCATE audit_log')


@pytest.fixture
def saved_result(pipeline_case, tmp_path):
    root, pdfs, invoices, answers, _ = pipeline_case
    client = FakeClient(pdfs, invoices, answers)
    start = datetime.now(timezone.utc)
    result = run_pipeline(pdfs, load_reference(root), client=client, audit_path=tmp_path / 'audit.jsonl',
                          models=PipelineModels(), batch=Budget(label='batch', max_calls=760,
                                                               max_tool_calls=380, max_cost='5'))
    import json
    records = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    metadata = RunMetadata(run_id=result.pipeline_run_id, started_at=start,
                           finished_at=datetime.now(timezone.utc),
                           model_ids={'extract': PipelineModels().extract,
                                      'policy': PipelineModels().policy,
                                      'investigate': PipelineModels().investigate},
                           document_hashes={n: sha256(pdf).hexdigest() for n, pdf in pdfs.items()},
                           git_revision='synthetic-test', audit_records=records)
    return result, metadata


def test_save_and_load_equal_findings_and_notes(db_owner, db_app, saved_result):
    result, metadata = saved_result
    save_run(db_app, result, metadata)
    loaded = load_findings(db_app, metadata.run_id)
    assert [entry.finding for entry in loaded] == result.findings
    notes = defaultdict(list)
    for entry in loaded:
        notes[entry.finding.invoice_numbers[0]].extend(entry.notes)
        assert isinstance(entry.finding.amount_at_issue, Decimal)
    assert dict(notes) == result.notes
    values = db_app.execute('''SELECT total, subtotal, tax, unit_price, amount FROM invoices
        JOIN line_items USING (invoice_id) WHERE run_id=%s''', (metadata.run_id,)).fetchall()
    assert values and all(isinstance(value, Decimal) for row in values for value in row)
    assert db_owner.execute('SELECT count(*) FROM audit_log WHERE pipeline_run_id=%s',
                            (metadata.run_id,)).fetchone()[0] == len(metadata.audit_records)
    assert db_owner.execute('SELECT count(*) FROM decisions d JOIN findings f USING (finding_id) WHERE f.run_id=%s',
                            (metadata.run_id,)).fetchone()[0] == 0
    hashes = dict(db_app.execute('SELECT document_id, pdf_sha256 FROM documents WHERE run_id=%s',
                                 (metadata.run_id,)).fetchall())
    assert hashes == metadata.document_hashes
    with pytest.raises(psycopg.errors.UniqueViolation):
        save_run(db_app, result, metadata)
    assert [entry.finding for entry in load_findings(db_app, metadata.run_id)] == result.findings


def test_failure_mid_save_rolls_everything_back(db_app, saved_result):
    result, metadata = saved_result
    broken = deepcopy(result)
    # Bypass Pydantic deliberately to exercise the actual database CHECK.
    broken.findings[-1] = broken.findings[-1].model_copy(update={'amount_at_issue': Decimal('-1')})
    with pytest.raises(psycopg.errors.CheckViolation):
        save_run(db_app, broken, metadata)
    for table in ('runs', 'documents', 'invoices', 'findings', 'referrals'):
        query = sql.SQL('SELECT count(*) FROM {} WHERE run_id=%s').format(sql.Identifier(table))
        assert db_app.execute(query, (metadata.run_id,)).fetchone()[0] == 0
    assert db_app.execute('SELECT count(*) FROM audit_log WHERE pipeline_run_id=%s',
                          (metadata.run_id,)).fetchone()[0] == 0
    # No dangling line items or notes can survive a rolled-back parent insert.
    assert not load_findings(db_app, metadata.run_id)


def test_decisions_are_append_only_even_for_owner(db_owner, db_app, saved_result):
    result, metadata = saved_result
    save_run(db_app, result, metadata)
    finding_id = load_findings(db_app, metadata.run_id)[0].finding_id
    for decision in ('approve', 'reject'):
        db_app.execute('INSERT INTO decisions (finding_id, reviewer, decision, note) VALUES (%s, %s, %s, %s)',
                       (finding_id, 'synthetic-reviewer', decision, 'Synthetic test decision'))
    assert db_app.execute('SELECT decision FROM decisions WHERE finding_id=%s ORDER BY decision_id',
                         (finding_id,)).fetchall() == [('approve',), ('reject',)]
    for conn in (db_app, db_owner):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE decisions SET note='edited' WHERE finding_id=%s", (finding_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('DELETE FROM decisions WHERE finding_id=%s', (finding_id,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute('TRUNCATE decisions')


def test_no_binary_pdf_columns(db_owner):
    # Inspect every user-table bytea column, including domains over bytea.
    columns = db_owner.execute('''SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema='public' AND (udt_name='bytea' OR data_type='bytea')''').fetchall()
    assert set(columns) == {('checkpoint_blobs', 'blob'), ('checkpoint_writes', 'blob')}
    for table, column in columns:
        query = sql.SQL('SELECT count(*) FROM {} WHERE substring({} from 1 for 4)=%s').format(
            sql.Identifier(table), sql.Identifier(column))
        assert db_owner.execute(query, (b'%PDF',)).fetchone()[0] == 0


def test_partial_run_referrals_and_missing_notes_saved(db_app, saved_result):
    result, metadata = saved_result
    result.notes.clear()
    result.referrals = [{'document_id': next(iter(result.invoices)), 'stage': 'budget',
                          'reason': 'max_cost_exceeded'}]
    result.stopped = 'max_cost_exceeded'
    result.skipped_stages = ['investigation']
    save_run(db_app, result, metadata)
    row = db_app.execute('SELECT status, stopped_reason, skipped_stages FROM runs WHERE run_id=%s',
                         (metadata.run_id,)).fetchone()
    assert row == ('stopped', 'max_cost_exceeded', ['investigation'])
    assert all(not entry.notes for entry in load_findings(db_app, metadata.run_id))
    assert db_app.execute('SELECT stage FROM referrals WHERE run_id=%s',
                         (metadata.run_id,)).fetchone()[0] == 'budget'


def test_late_audit_import_failure_rolls_back_result_and_prior_audit_rows(db_app, saved_result):
    result, metadata = saved_result
    malformed = {**audit_record(), 'decision': 'invalid'}
    metadata = replace(metadata, audit_records=[audit_record(), malformed])
    with pytest.raises(psycopg.errors.CheckViolation):
        save_run(db_app, result, metadata)
    assert db_app.execute('SELECT count(*) FROM runs WHERE run_id=%s', (metadata.run_id,)).fetchone()[0] == 0
    assert db_app.execute('SELECT count(*) FROM audit_log WHERE pipeline_run_id=%s',
                          (metadata.run_id,)).fetchone()[0] == 0


def test_notes_keep_group_indexes_when_multiple_findings_share_first_invoice(db_app, saved_result):
    result, metadata = saved_result
    finding = deepcopy(result.findings[0])
    number = finding.invoice_numbers[0]
    original_group = [f for f in result.findings if f.invoice_numbers[0] == number]
    note = result.notes[number][0].model_copy(update={'finding_index': len(original_group)})
    result.findings.append(finding)
    result.notes[number].append(note)
    save_run(db_app, result, metadata)
    loaded = load_findings(db_app, metadata.run_id)
    assert [entry.finding for entry in loaded] == result.findings
    group_notes = [n for entry in loaded if entry.finding.invoice_numbers[0] == number for n in entry.notes]
    assert group_notes == result.notes[number]


def test_review_status_is_derived_and_not_directly_writable(db_app, saved_result):
    result, metadata = saved_result
    # Even a supplied in-memory status cannot bypass the decisions ledger.
    result.findings[0] = result.findings[0].model_copy(update={'status': 'approved'})
    save_run(db_app, result, metadata)
    entry = load_findings(db_app, metadata.run_id)[0]
    finding_id = entry.finding_id
    assert entry.finding.status == 'pending'
    assert db_app.execute('SELECT status FROM finding_review_status WHERE finding_id=%s',
                          (finding_id,)).fetchone()[0] == 'pending'
    for decision, expected in [('approve', 'approved'), ('reject', 'rejected')]:
        db_app.execute('''INSERT INTO decisions (finding_id, reviewer, decision, note)
            VALUES (%s, %s, %s, %s)''',
                       (finding_id, 'synthetic-reviewer', decision, 'Synthetic test decision'))
        assert load_findings(db_app, metadata.run_id)[0].finding.status == expected
    assert all(entry.finding.status == 'pending' for entry in load_findings(db_app, metadata.run_id)[1:])
    assert db_app.execute("SELECT has_table_privilege(current_user, 'findings', 'UPDATE')").fetchone()[0] is False
    assert db_app.execute("SELECT has_any_column_privilege(current_user, 'findings', 'UPDATE')").fetchone()[0] is False
    for command in ["UPDATE findings SET status='approved' WHERE finding_id=%s",
                    "INSERT INTO findings (status) VALUES ('approved')"]:
        with pytest.raises(psycopg.errors.UndefinedColumn):
            db_app.execute(command, (finding_id,)) if '%s' in command else db_app.execute(command)
    for privilege in ('INSERT', 'UPDATE', 'DELETE'):
        assert db_app.execute("SELECT has_table_privilege(current_user, 'finding_review_status', %s)",
                              (privilege,)).fetchone()[0] is False
    for command in ["UPDATE finding_review_status SET status='approved' WHERE finding_id=%s",
                    "INSERT INTO finding_review_status (finding_id, status) VALUES (%s, 'approved')"]:
        with pytest.raises(psycopg.Error):
            db_app.execute(command, (finding_id,))
    assert load_findings(db_app, metadata.run_id)[0].finding.status == 'rejected'


def test_app_cannot_supply_decision_time(db_app, saved_result):
    result, metadata = saved_result
    save_run(db_app, result, metadata)
    finding_id = load_findings(db_app, metadata.run_id)[0].finding_id
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        db_app.execute('''INSERT INTO decisions (finding_id, reviewer, decision, note, time)
            VALUES (%s, %s, %s, %s, %s)''',
            (finding_id, 'synthetic-reviewer', 'approve', 'Synthetic note', datetime.now(timezone.utc)))
    assert load_findings(db_app, metadata.run_id)[0].finding.status == 'pending'
    db_app.execute('''INSERT INTO decisions (finding_id, reviewer, decision, note)
        VALUES (%s, %s, %s, %s)''', (finding_id, 'synthetic-reviewer', 'approve', 'Synthetic note'))
    assert db_app.execute('SELECT time IS NOT NULL FROM decisions WHERE finding_id=%s',
                          (finding_id,)).fetchone()[0] is True
    assert load_findings(db_app, metadata.run_id)[0].finding.status == 'approved'


@pytest.mark.parametrize('decisions,expected', [(('approve', 'reject'), 'rejected'),
                                               (('reject', 'approve'), 'approved')])
def test_latest_decision_uses_id_regardless_of_time(db_owner, db_app, saved_result, decisions, expected):
    result, metadata = saved_result
    save_run(db_app, result, metadata)
    finding_id = load_findings(db_app, metadata.run_id)[0].finding_id
    # Only the owner can supply historical times. Reverse chronological times
    # deliberately disagree with insert order; no existing decision is edited.
    for decision, year in zip(decisions, (2030, 2020)):
        db_owner.execute('''INSERT INTO decisions (finding_id, reviewer, decision, note, time)
            VALUES (%s, %s, %s, %s, %s)''', (finding_id, 'synthetic-reviewer', decision,
            'Synthetic historical decision', datetime(year, 1, 1, tzinfo=timezone.utc)))
    assert load_findings(db_app, metadata.run_id)[0].finding.status == expected


def test_app_decision_transitions_follow_append_order(db_app, saved_result):
    result, metadata = saved_result
    save_run(db_app, result, metadata)
    finding_id = load_findings(db_app, metadata.run_id)[0].finding_id
    for decision, expected in [('approve', 'approved'), ('reject', 'rejected'), ('approve', 'approved')]:
        db_app.execute('''INSERT INTO decisions (finding_id, reviewer, decision, note)
            VALUES (%s, %s, %s, %s)''', (finding_id, 'synthetic-reviewer', decision, 'Synthetic note'))
        assert load_findings(db_app, metadata.run_id)[0].finding.status == expected


def test_save_refuses_mismatched_pipeline_run_id(db_app, saved_result):
    result, metadata = saved_result
    wrong = replace(metadata, run_id='wrong-' + uuid4().hex)
    with pytest.raises(ValueError, match='must match result.pipeline_run_id'):
        save_run(db_app, result, wrong)
    assert db_app.execute('SELECT count(*) FROM runs WHERE run_id=ANY(%s)',
                          ([metadata.run_id, wrong.run_id],)).fetchone()[0] == 0
