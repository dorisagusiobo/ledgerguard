"""Offline tool authorization, append-only audit phases, and shared budgets."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from ledgerguard.gateway import Budget, Gateway, GatewayBlocked
from ledgerguard.tools import ReadOnlyTools


class FakeTools:
    def __init__(self, on_call=None, error=False):
        self.calls = []
        self.on_call, self.error = on_call, error

    def vendor_history(self, **arguments):
        if self.on_call:
            self.on_call()
        self.calls.append(arguments)
        if self.error:
            raise RuntimeError('PRIVATE-VENDOR PRIVATE-RESULT')
        return {'rows': [{'month': '2025-01', 'total': '99.00'}], 'truncated': False}


def gateway(tmp_path, *, budget=None, shared_budgets=()):
    return Gateway(client=None, budget=budget or Budget(), run_id='TOOL-RUN-001',
                   audit_path=tmp_path / 'audit.jsonl', shared_budgets=shared_budgets)


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]


def test_unknown_tool_blocked_counted_not_run(tmp_path):
    gw, tools = gateway(tmp_path), FakeTools()
    with pytest.raises(GatewayBlocked, match='tool_not_allowed'):
        gw.call_tool('pay_invoice', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert not tools.calls and gw.budget.tool_calls == 1
    record, = records(tmp_path)
    assert record['decision'] == 'block' and record['reason'] == 'tool_not_allowed'


def test_invalid_arguments_consume_cap(tmp_path):
    gw, tools = gateway(tmp_path, budget=Budget(max_tool_calls=1)), FakeTools()
    with pytest.raises(GatewayBlocked, match='invalid_tool_arguments'):
        gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR', 'extra': 1}, tools)
    with pytest.raises(GatewayBlocked, match='max_tool_calls_exceeded'):
        gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert not tools.calls and gw.budget.tool_calls == 2
    assert len(records(tmp_path)) == 2


def test_eleventh_attempt_is_blocked(tmp_path):
    gw, tools = gateway(tmp_path), FakeTools()
    for _ in range(10):
        gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    with pytest.raises(GatewayBlocked, match='max_tool_calls_exceeded'):
        gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert len(tools.calls) == 10 and gw.budget.tool_calls == 11
    assert len(records(tmp_path)) == 21
    assert gw.budget.calls == 0 and gw.budget.cost == 0


def test_authorization_precedes_execution_and_completion_has_count(tmp_path):
    gw = gateway(tmp_path)

    def before_call():
        record, = records(tmp_path)
        assert record['phase'] == 'authorization' and record['outcome'] == 'authorized'
        assert record['decision'] == 'allow' and record['row_count'] is None

    tools = FakeTools(on_call=before_call)
    result = gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert result['rows']
    authorization, completion = records(tmp_path)
    assert authorization['call_id'] == completion['call_id']
    assert completion['outcome'] == 'success' and completion['row_count'] == 1
    assert completion['phase'] == 'completion'
    text = (tmp_path / 'audit.jsonl').read_text()
    assert 'PRIVATE-VENDOR' not in text and '99.00' not in text and '2025-01' not in text
    assert 'arguments' not in text and 'rows' not in text


def test_tool_error_logged_without_details_and_slot_retained(tmp_path):
    gw, tools = gateway(tmp_path), FakeTools(error=True)
    with pytest.raises(RuntimeError, match='Read-only tool failed') as error:
        gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert 'PRIVATE' not in str(error.value)
    assert records(tmp_path)[-1]['outcome'] == 'tool_error'
    assert 'PRIVATE' not in (tmp_path / 'audit.jsonl').read_text()
    assert gw.budget.tool_calls == 1


def test_shared_tool_cap_across_gateway_instances(tmp_path):
    shared = Budget(max_tool_calls=1)
    first = gateway(tmp_path, shared_budgets=(shared,))
    second = gateway(tmp_path, shared_budgets=(shared,))
    tools = FakeTools()
    first.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    with pytest.raises(GatewayBlocked, match='max_tool_calls_exceeded'):
        second.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert len(tools.calls) == 1 and shared.tool_calls == 2
    assert first.budget.tool_calls == second.budget.tool_calls == 1


def test_concurrent_shared_cap_allows_one_execution(tmp_path):
    shared = Budget(max_tool_calls=1)
    gateways = [gateway(tmp_path, shared_budgets=(shared,)) for _ in range(2)]
    tools = FakeTools()

    def attempt(gw):
        try:
            gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
            return True
        except GatewayBlocked:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, gateways)) == 1
    assert len(tools.calls) == 1


def test_unwritable_log_stops_execution(tmp_path):
    gw, tools = gateway(tmp_path), FakeTools()
    (tmp_path / 'audit.jsonl').mkdir()
    with pytest.raises(OSError):
        gw.call_tool('vendor_history', {'vendor_name': 'PRIVATE-VENDOR'}, tools)
    assert not tools.calls


def test_gateway_dispatches_all_real_tools(tmp_path):
    gw, tools = gateway(tmp_path), ReadOnlyTools([], [])
    args = {'vendor_history': {'vendor_name': 'Unknown'},
            'related_invoices': {'vendor_name': 'Unknown', 'start': '2026-01-01', 'end': '2026-01-31'},
            'duplicate_search': {'invoice_number': 'INV-UNKNOWN'}}
    for name, arguments in args.items():
        assert gw.call_tool(name, arguments, tools) == {'rows': [], 'truncated': False}
    assert len(records(tmp_path)) == 6


def test_invalid_tool_budget_rejected():
    for value in [-1, True, 1.5]:
        with pytest.raises(ValueError):
            Budget(max_tool_calls=value)
