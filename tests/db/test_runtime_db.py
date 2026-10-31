"""Durable audit, cross-run monthly accounting and strict checkpoint round trips."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from itertools import count
from threading import Barrier
from uuid import uuid4

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
import psycopg
from psycopg.rows import dict_row
import pytest

from fakes import FakeClient
from ledgerguard.audit import JsonlSink, PostgresSink
from ledgerguard.db import CHECKPOINT_TABLES, setup_checkpoints
from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, MODEL_ID, Prices
from ledgerguard.investigator import investigate
from ledgerguard.monthly import MonthlyLedger
from ledgerguard.pipeline import PipelineModels, run_pipeline
from ledgerguard.rules import load_reference, run_rules
from ledgerguard.tools import ReadOnlyTools


class Client:
    def __init__(self, *, usage=None, error=False):
        self.calls = 0
        self.usage = usage if usage is not None else {'inputTokens': 100, 'outputTokens': 100}
        self.error = error

    def converse(self, **request):
        self.calls += 1
        if self.error:
            raise RuntimeError('synthetic failure')
        return {'usage': self.usage}


def request():
    return {'messages': [], 'inferenceConfig': {'maxTokens': 100}}


def gateway(tmp_path, *, client=None, monthly=None, audit=None, **kwargs):
    return Gateway(client=client or Client(), budget=Budget(), run_id='test-' + uuid4().hex,
                   audit=audit or JsonlSink(tmp_path / 'audit.jsonl'), monthly=monthly,
                   prices={MODEL_ID: Prices(Decimal('100'), Decimal('100'))},
                   input_estimator=lambda r: 100, **kwargs)  # Reserves exactly $0.02.


months = count()


@pytest.fixture
def ledger_case(db_owner, app_conninfo):
    index = next(months)
    now = datetime(2080 + index // 12, index % 12 + 1, 31 if index % 12 == 0 else 1, 23, 59, tzinfo=timezone.utc)
    clock = [now]
    ledger = MonthlyLedger(app_conninfo, clock=lambda: clock[0])
    month = now.date().replace(day=1)
    db_owner.execute('INSERT INTO monthly_spend (month) VALUES (%s)', (month,))
    return ledger, clock, month


def balances(conn, month):
    return conn.execute('SELECT reserved, spent FROM monthly_spend WHERE month=%s', (month,)).fetchone()


def test_postgres_audit_survives_caller_rollback(db_owner, app_conninfo, tmp_path):
    gw = gateway(tmp_path, audit=PostgresSink(app_conninfo))
    with pytest.raises(RuntimeError, match='rollback caller'):
        with db_owner.transaction():
            db_owner.execute('CREATE TABLE rollback_only (id INTEGER)')
            gw.converse(MODEL_ID, request())
            raise RuntimeError('rollback caller')
    rows = db_owner.execute('SELECT outcome FROM audit_log WHERE run_id=%s ORDER BY audit_id',
                             (gw.run_id,)).fetchall()
    assert rows == [('authorized',), ('success',)]
    assert db_owner.execute("SELECT to_regclass('rollback_only')").fetchone()[0] is None


def test_postgres_audit_authorized_before_client(db_owner, app_conninfo, tmp_path):
    class ObservingClient(Client):
        def converse(self, **request):
            assert db_owner.execute('SELECT outcome FROM audit_log WHERE run_id=%s',
                                    (gw.run_id,)).fetchall() == [('authorized',)]
            return super().converse(**request)
    gw = gateway(tmp_path, client=ObservingClient(), audit=PostgresSink(app_conninfo))
    gw.converse(MODEL_ID, request())


def test_unavailable_audit_blocks_before_client(tmp_path):
    client = Client()
    gw = gateway(tmp_path, client=client,
                 audit=PostgresSink('host=127.0.0.1 port=1 dbname=missing user=ledgerguard_app connect_timeout=1'))
    with pytest.raises(OSError):
        gw.converse(MODEL_ID, request())
    assert client.calls == 0 and gw.budget.calls == 0


def test_runtime_connections_refuse_owner(postgres_database, tmp_path):
    client = Client()
    gw = gateway(tmp_path, client=client, audit=PostgresSink(postgres_database))
    with pytest.raises(OSError, match='ledgerguard_app'):
        gw.converse(MODEL_ID, request())
    assert client.calls == 0


def test_monthly_blocks_at_limit(db_owner, ledger_case, tmp_path):
    ledger, _, month = ledger_case
    db_owner.execute('UPDATE monthly_spend SET spent=9.99 WHERE month=%s', (month,))
    client = Client()
    gw = gateway(tmp_path, client=client, monthly=ledger)
    with pytest.raises(GatewayBlocked) as caught:
        gw.converse(MODEL_ID, request())
    assert str(caught.value) == 'max_cost_exceeded' and caught.value.scope == 'monthly'
    assert client.calls == 0
    assert balances(db_owner, month) == (Decimal(0), Decimal('9.99'))


def test_two_threads_race_for_last_reservation(db_owner, ledger_case):
    ledger, _, month = ledger_case
    db_owner.execute('UPDATE monthly_spend SET spent=9.98 WHERE month=%s', (month,))
    barrier = Barrier(2)
    def reserve():
        barrier.wait(timeout=5)
        try:
            return ledger.reserve(Decimal('0.02'))
        except GatewayBlocked as error:
            assert error.scope == 'monthly'
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        reservations = list(pool.map(lambda _: reserve(), range(2)))
    assert sum(r is not None for r in reservations) == 1
    assert balances(db_owner, month) == (Decimal('0.02'), Decimal('9.98'))


def test_month_boundary_charges_original_month(db_owner, app_conninfo, tmp_path):
    clock = [datetime(2090, 10, 31, 23, 59, tzinfo=timezone.utc)]
    ledger = MonthlyLedger(app_conninfo, clock=lambda: clock[0])
    class CrossingClient(Client):
        def converse(self, **request):
            clock[0] = datetime(2090, 11, 1, tzinfo=timezone.utc)
            return super().converse(**request)
    gw = gateway(tmp_path, client=CrossingClient(), monthly=ledger)
    gw.converse(MODEL_ID, request())
    assert balances(db_owner, datetime(2090, 10, 1).date()) == (Decimal(0), Decimal('0.02'))
    assert balances(db_owner, datetime(2090, 11, 1).date()) is None


def test_crashed_reservation_remains_counted(db_owner, ledger_case):
    ledger, _, month = ledger_case
    db_owner.execute('UPDATE monthly_spend SET spent=9.98 WHERE month=%s', (month,))
    ledger.reserve('0.02')  # Deliberately never reconcile; simulate process loss.
    with pytest.raises(GatewayBlocked) as caught:
        ledger.reserve('0.01')
    assert caught.value.scope == 'monthly'
    assert balances(db_owner, month) == (Decimal('0.02'), Decimal('9.98'))


@pytest.mark.parametrize('outcome,spent', [('success', '0.01'), ('client_error', '0'), ('invalid_usage', '0.02')])
def test_monthly_reconciliation(db_owner, ledger_case, tmp_path, outcome, spent):
    ledger, _, month = ledger_case
    client = Client(usage={} if outcome == 'invalid_usage' else {'inputTokens': 50, 'outputTokens': 50},
                    error=outcome == 'client_error')
    gw = gateway(tmp_path, client=client, monthly=ledger)
    if outcome == 'success':
        gw.converse(MODEL_ID, request())
    else:
        with pytest.raises(RuntimeError):
            gw.converse(MODEL_ID, request())
    assert balances(db_owner, month) == (Decimal(0), Decimal(spent))


def test_invoice_block_releases_monthly_reservation(db_owner, ledger_case, tmp_path):
    ledger, _, month = ledger_case
    gw = gateway(tmp_path, monthly=ledger)
    gw.budget.max_calls = 0
    with pytest.raises(GatewayBlocked) as caught:
        gw.converse(MODEL_ID, request())
    assert caught.value.scope is None
    assert balances(db_owner, month) == (Decimal(0), Decimal(0))


def test_monthly_limit_has_priority(db_owner, ledger_case, tmp_path):
    ledger, _, month = ledger_case
    db_owner.execute('UPDATE monthly_spend SET spent=10 WHERE month=%s', (month,))
    gw = gateway(tmp_path, monthly=ledger)
    gw.budget.max_calls = 0
    with pytest.raises(GatewayBlocked) as caught:
        gw.converse(MODEL_ID, request())
    assert caught.value.scope == 'monthly'


def test_spend_permissions(db_app, ledger_case):
    _, _, month = ledger_case
    for statement, args in [
        ('UPDATE spend_limits SET monthly_limit=100', ()),
        ('DELETE FROM spend_limits', ()),
        ('INSERT INTO spend_limits (monthly_limit) VALUES (100)', ()),
        ('UPDATE monthly_spend SET month=%s WHERE month=%s', (month, month)),
        ('INSERT INTO monthly_spend (month, spent) VALUES (%s, 0)', (month,)),
        ('DELETE FROM monthly_spend WHERE month=%s', (month,))]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_app.execute(statement, args)


def test_pipeline_monthly_block_returns_skipped_stages(db_owner, ledger_case, pipeline_case, tmp_path):
    ledger, _, month = ledger_case
    db_owner.execute('UPDATE monthly_spend SET spent=10 WHERE month=%s', (month,))
    root, pdfs, invoices, answers, _ = pipeline_case
    client = FakeClient(pdfs, invoices, answers)
    result = run_pipeline(pdfs, load_reference(root), client=client, audit_path=tmp_path / 'audit.jsonl',
                          models=PipelineModels(), batch=Budget(label='batch', max_calls=760,
                                                               max_tool_calls=380, max_cost='5'), monthly=ledger)
    assert result.stopped == 'max_cost_exceeded'
    assert result.skipped_stages == ['extraction', 'rules', 'policy', 'investigation']
    assert client.calls == []


def test_checkpoint_setup_owner_only(db_owner, db_app, monkeypatch):
    calls = []
    original = PostgresSaver.setup
    def setup(saver):
        assert saver.conn.execute('SELECT current_user AS role').fetchone()['role'] != 'ledgerguard_app'
        calls.append(1)
        return original(saver)
    monkeypatch.setattr(PostgresSaver, 'setup', setup)
    setup_checkpoints(db_owner)
    assert calls == [1]
    with pytest.raises(PermissionError):
        setup_checkpoints(db_app)
    assert calls == [1]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        original(PostgresSaver(db_app))


def test_checkpoint_state_round_trip_and_permissions(app_conninfo, db_app, pipeline_case, tmp_path):
    root, pdfs, invoices, answers, _ = pipeline_case
    reference = load_reference(root)
    findings = run_rules(invoices, reference.contracts, reference.history, reference.policy_clauses).findings
    number = findings[0].invoice_numbers[0]
    group = [f for f in findings if f.invoice_numbers[0] == number]
    invoice = next(i for i in invoices if i.invoice_number == number)
    client = FakeClient(pdfs, invoices, answers, tool=True)
    gw = Gateway(client=client, budget=Budget(), run_id='checkpoint-' + uuid4().hex,
                 audit=PostgresSink(app_conninfo))
    thread_id = f'{gw.run_id}:{number}'
    # Strict deserialization allows no application dataclasses or Pydantic types.
    serde = JsonPlusSerializer(allowed_msgpack_modules=[])
    with psycopg.connect(app_conninfo, autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
        saver = PostgresSaver(conn, serde=serde)
        result = investigate(invoice, group, tools=ReadOnlyTools(invoices, reference.history),
                             gateway=gw, model_id=MODEL_ID, checkpointer=saver)
        saved = saver.get_tuple({'configurable': {'thread_id': thread_id}})
        state = saved.checkpoint['channel_values']
        assert state['outcome'] == {'notes': [n.model_dump(mode='json') for n in result.notes], 'needs_review': None}
        assert isinstance(state['seen'], list) and number in state['seen']
        assert state['turns'] == 2 and state['route'] == 'finish'
    with psycopg.connect(app_conninfo, autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
        restored = PostgresSaver(conn, serde=serde).get_tuple({'configurable': {'thread_id': thread_id}})
        assert restored.checkpoint['channel_values'] == state
    for table in CHECKPOINT_TABLES:
        for privilege in ('SELECT', 'INSERT', 'UPDATE'):
            assert db_app.execute('SELECT has_table_privilege(current_user, %s, %s)',
                                  (table, privilege)).fetchone()[0]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db_app.execute(f'DELETE FROM {table}')


def test_monthly_database_unavailable_stops_before_client(tmp_path):
    client = Client()
    ledger = MonthlyLedger('host=127.0.0.1 port=1 dbname=missing user=ledgerguard_app connect_timeout=1')
    gw = gateway(tmp_path, client=client, monthly=ledger)
    with pytest.raises(OSError):
        gw.converse(MODEL_ID, request())
    assert client.calls == 0 and gw.budget.calls == 0


def test_failed_durable_authorization_releases_reservation(db_owner, ledger_case, tmp_path):
    from contextlib import contextmanager
    ledger, _, month = ledger_case
    class BrokenSink:
        @contextmanager
        def session(self):
            yield self
        def authorize(self, record):
            raise OSError('synthetic audit failure')
        def append(self, record):
            raise AssertionError('No final record without authorization')
    client = Client()
    gw = gateway(tmp_path, client=client, monthly=ledger, audit=BrokenSink())
    with pytest.raises(OSError):
        gw.converse(MODEL_ID, request())
    assert balances(db_owner, month) == (Decimal(0), Decimal(0))
    assert client.calls == 0 and gw.budget.calls == 0


def test_postgres_tool_audits_have_one_row_per_record(db_owner, app_conninfo, tmp_path):
    gw = gateway(tmp_path, audit=PostgresSink(app_conninfo))
    tools = ReadOnlyTools([], [])
    gw.call_tool('vendor_history', {'vendor_name': 'Synthetic vendor'}, tools)
    with pytest.raises(GatewayBlocked):
        gw.call_tool('not_allowed', {}, tools)
    records = db_owner.execute('SELECT phase, outcome FROM audit_log WHERE run_id=%s ORDER BY audit_id',
                               (gw.run_id,)).fetchall()
    assert records == [('authorization', 'authorized'), ('completion', 'success'), ('authorization', 'blocked')]


def test_pipeline_uses_postgres_audit_ledger_and_checkpoints_together(db_owner, app_conninfo, ledger_case, pipeline_case):
    ledger, _, month = ledger_case
    root, pdfs, invoices, answers, _ = pipeline_case
    selected = {number: pdfs[number] for number in sorted(pdfs)[:4]}
    client = FakeClient(pdfs, invoices, answers)
    with psycopg.connect(app_conninfo, autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
        saver = PostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        result = run_pipeline(selected, load_reference(root), client=client,
                              audit=PostgresSink(app_conninfo), monthly=ledger, checkpointer=saver,
                              models=PipelineModels(), batch=Budget(label='batch', max_calls=80,
                                                                   max_tool_calls=40, max_cost='0.50'))
        assert len(result.findings) == len(result.notes) == 2
        assert not result.referrals and result.stopped is None
        for number in result.notes:
            state = saver.get_tuple({'configurable': {'thread_id': f'{result.pipeline_run_id}:{number}:{number}'}})
            assert state.checkpoint['channel_values']['outcome']['notes'] == [
                n.model_dump(mode='json') for n in result.notes[number]]
    assert balances(db_owner, month) == (Decimal(0), sum((b['cost'] for b in result.budgets.values()), Decimal(0)))


def test_repeated_pdfs_get_distinct_audit_and_checkpoint_ids(db_owner, app_conninfo, pipeline_case):
    root, pdfs, invoices, answers, _ = pipeline_case
    selected = {number: pdfs[number] for number in sorted(pdfs)[:4]}
    results = []
    threads = []
    with psycopg.connect(app_conninfo, autocommit=True, row_factory=dict_row, prepare_threshold=0) as conn:
        saver = PostgresSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        for _ in range(2):
            result = run_pipeline(selected, load_reference(root), client=FakeClient(pdfs, invoices, answers),
                                  audit=PostgresSink(app_conninfo), checkpointer=saver,
                                  models=PipelineModels(), batch=Budget(label='batch', max_calls=80,
                                                                       max_tool_calls=40, max_cost='0.50'))
            assert len(result.notes) == 2 and result.stopped is None
            results.append(result)
            run_threads = {f'{result.pipeline_run_id}:{number}:{number}' for number in result.notes}
            assert all(saver.get_tuple({'configurable': {'thread_id': thread}}) is not None
                       for thread in run_threads)
            threads.append(run_threads)
    assert len(results[0].pipeline_run_id) == len(results[1].pipeline_run_id) == 32
    assert results[0].pipeline_run_id != results[1].pipeline_run_id
    assert threads[0].isdisjoint(threads[1])
    for result in results:
        ids = {row[0] for row in db_owner.execute('SELECT DISTINCT run_id FROM audit_log WHERE run_id LIKE %s',
                                                  (result.pipeline_run_id + ':%',)).fetchall()}
        assert ids == {f'{result.pipeline_run_id}:{number}' for number in selected}
