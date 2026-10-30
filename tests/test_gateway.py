"""Offline gateway accounting; clients never contact AWS."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, MODEL_ID, Prices


class FakeClient:
    def __init__(self, *, usage=None, error=False):
        self.calls = []
        self.usage = usage if usage is not None else {'inputTokens': 100, 'outputTokens': 10}
        self.error = error

    def converse(self, **request):
        self.calls.append(request)
        if self.error:
            raise RuntimeError('PRIVATE PROMPT echoed by provider')
        return {'usage': self.usage, 'output': {'message': {'content': [{'text': 'PRIVATE RESPONSE'}]}}}


@pytest.fixture
def request_data():
    return {'messages': [{'role': 'user', 'content': [{'text': 'PRIVATE PROMPT'}]}],
            'inferenceConfig': {'maxTokens': 100}}


def gateway(tmp_path, *, client=None, budget=None, **kwargs):
    return Gateway(client=client or FakeClient(), budget=budget or Budget(),
                   run_id='RUN-2026-0001', audit_path=tmp_path / 'audit.jsonl',
                   input_estimator=lambda request: 100, **kwargs)


def records(tmp_path):
    return [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]


def test_unknown_model_never_called_and_logged(tmp_path, request_data):
    gw = gateway(tmp_path)
    with pytest.raises(GatewayBlocked, match='model_not_allowed'):
        gw.converse('unknown-model', request_data)
    assert gw.client.calls == []
    assert gw.budget.calls == 0 and gw.budget.cost == 0
    assert records(tmp_path)[0]['decision'] == 'block'


def test_call_limit_blocks_before_client(tmp_path, request_data):
    gw = gateway(tmp_path, budget=Budget(max_calls=1))
    gw.converse(MODEL_ID, request_data)
    with pytest.raises(GatewayBlocked, match='max_calls'):
        gw.converse(MODEL_ID, request_data)
    assert len(gw.client.calls) == gw.budget.calls == 1
    assert [r['decision'] for r in records(tmp_path)] == ['allow', 'block']


@pytest.mark.parametrize('limit,allowed', [('0.001799', False), ('0.001800', True)])
def test_cost_reservation_boundary(tmp_path, request_data, limit, allowed):
    # 100 estimated input * $3/M + 100 max output * $15/M = $0.0018.
    gw = gateway(tmp_path, budget=Budget(max_cost=Decimal(limit)))
    if allowed:
        gw.converse(MODEL_ID, request_data)
    else:
        with pytest.raises(GatewayBlocked, match='max_cost'):
            gw.converse(MODEL_ID, request_data)
    assert len(gw.client.calls) == int(allowed)


def test_reserve_before_invocation_and_reconcile_actual_usage(tmp_path, request_data):
    budget = Budget()

    class ObservingClient(FakeClient):
        def converse(self, **request):
            assert budget.cost == Decimal('0.0018')
            assert budget.calls == 1
            return super().converse(**request)

    gw = gateway(tmp_path, client=ObservingClient(), budget=budget)
    response = gw.converse(MODEL_ID, request_data)
    assert response['usage']['inputTokens'] == 100
    assert budget.cost == Decimal('0.00045')
    assert isinstance(budget.cost, Decimal)
    assert gw.client.calls[0]['modelId'] == MODEL_ID
    record, = records(tmp_path)
    assert record['input_tokens'] == 100 and record['output_tokens'] == 10
    assert Decimal(record['cost']) == budget.cost
    assert Decimal(record['reserved_cost']) == Decimal('0.0018')


def test_audit_append_only_one_record_per_attempt_without_contents(tmp_path, request_data):
    gw = gateway(tmp_path)
    gw.converse(MODEL_ID, request_data)
    first = (tmp_path / 'audit.jsonl').read_bytes()
    with pytest.raises(GatewayBlocked):
        gw.converse('unknown', request_data)
    assert (tmp_path / 'audit.jsonl').read_bytes().startswith(first)
    assert len(records(tmp_path)) == 2
    text = (tmp_path / 'audit.jsonl').read_text()
    assert 'PRIVATE' not in text
    assert all(r['time'] and r['run_id'] == 'RUN-2026-0001' for r in records(tmp_path))


def test_client_error_logged_and_cost_released_but_attempt_counts(tmp_path, request_data):
    gw = gateway(tmp_path, client=FakeClient(error=True), budget=Budget(max_calls=1))
    with pytest.raises(RuntimeError, match='reservation released') as error:
        gw.converse(MODEL_ID, request_data)
    assert 'PRIVATE' not in str(error.value)
    assert gw.budget.calls == 1 and gw.budget.cost == 0
    record, = records(tmp_path)
    assert record['outcome'] == 'client_error' and Decimal(record['cost']) == 0
    assert 'PRIVATE' not in (tmp_path / 'audit.jsonl').read_text()
    with pytest.raises(GatewayBlocked, match='max_calls'):
        gw.converse(MODEL_ID, request_data)
    assert len(gw.client.calls) == 1


def test_shared_monthly_or_evaluation_budget_enforced(tmp_path, request_data):
    shared = Budget(max_calls=100, max_cost=Decimal('0.002'))
    first = gateway(tmp_path, shared_budgets=(shared,))
    first.converse(MODEL_ID, request_data)
    second = gateway(tmp_path, shared_budgets=(shared,))
    with pytest.raises(GatewayBlocked, match='max_cost'):
        second.converse(MODEL_ID, request_data)
    assert len(first.client.calls) == 1 and second.client.calls == []
    assert shared.calls == 1 and shared.cost == Decimal('0.00045')


def test_shared_reservation_blocks_concurrent_overspend(tmp_path, request_data):
    shared = Budget(max_calls=1)
    gateways = [gateway(tmp_path, shared_budgets=(shared,)) for _ in range(2)]

    def attempt(gw):
        try:
            gw.converse(MODEL_ID, request_data)
            return True
        except GatewayBlocked:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(attempt, gateways)) == 1
    assert sum(len(gw.client.calls) for gw in gateways) == 1
    assert len(records(tmp_path)) == 2


def test_real_usage_above_estimate_is_charged_and_stops_next_call(tmp_path, request_data):
    gw = gateway(tmp_path, client=FakeClient(usage={'inputTokens': 1000, 'outputTokens': 100}),
                 budget=Budget(max_cost=Decimal('0.002')))
    gw.converse(MODEL_ID, request_data)
    assert gw.budget.cost == Decimal('0.0045')
    assert records(tmp_path)[0]['reason'] == 'usage_exceeded_estimate'
    with pytest.raises(GatewayBlocked):
        gw.converse(MODEL_ID, request_data)


def test_missing_usage_retains_reservation(tmp_path, request_data):
    gw = gateway(tmp_path, client=FakeClient(usage={}))
    with pytest.raises(RuntimeError, match='reservation retained'):
        gw.converse(MODEL_ID, request_data)
    assert gw.budget.cost == Decimal('0.0018')
    assert records(tmp_path)[0]['outcome'] == 'invalid_usage'


@pytest.mark.parametrize('change', ['missing_max', 'zero_max', 'cache', 'tier', 'model_override'])
def test_unsupported_request_blocked_and_logged(tmp_path, request_data, change):
    if change == 'missing_max':
        request_data.pop('inferenceConfig')
    elif change == 'zero_max':
        request_data['inferenceConfig']['maxTokens'] = 0
    elif change == 'cache':
        request_data['system'] = [{'cachePoint': {'type': 'default'}}]
    elif change == 'tier':
        request_data['serviceTier'] = {'type': 'priority'}
    else:
        request_data['modelId'] = 'other'
    gw = gateway(tmp_path)
    with pytest.raises(GatewayBlocked):
        gw.converse(MODEL_ID, request_data)
    assert not gw.client.calls
    assert len(records(tmp_path)) == 1


def test_unwritable_audit_path_stops_invocation(tmp_path, request_data):
    gw = gateway(tmp_path)
    (tmp_path / 'audit.jsonl').mkdir()
    with pytest.raises(OSError):
        gw.converse(MODEL_ID, request_data)
    assert not gw.client.calls and gw.budget.calls == 0


def test_audit_path_must_be_outside_data(tmp_path):
    with pytest.raises(ValueError, match='outside data'):
        Gateway(client=FakeClient(), budget=Budget(), run_id='RUN-1', audit_path=tmp_path / 'data/audit.jsonl')


def test_prices_and_budgets_reject_float_and_invalid_values():
    with pytest.raises(ValueError):
        Budget(max_cost=0.5)
    with pytest.raises(ValueError):
        Prices(3.0, Decimal('15'))
    with pytest.raises(ValueError):
        Budget(max_calls=-1)
    with pytest.raises(ValueError):
        Budget(max_cost=Decimal('NaN'))
    assert Prices(Decimal('3'), Decimal('15')).cost(100, 10) == Decimal('0.00045')


def test_sdk_retries_must_not_bypass_gateway(tmp_path):
    client = FakeClient()
    client.meta = SimpleNamespace(config=SimpleNamespace(retries={'total_max_attempts': 3}))
    with pytest.raises(ValueError, match='total_max_attempts=1'):
        gateway(tmp_path, client=client)


def test_nova_global_profile_allowed_with_verified_prices(tmp_path, request_data):
    from ledgerguard.gateway import DEFAULT_PRICES, NOVA_MODEL_ID

    prices = DEFAULT_PRICES[NOVA_MODEL_ID]
    assert prices.input_per_million == Decimal('0.30')
    assert prices.output_per_million == Decimal('2.50')
    gw = gateway(tmp_path)
    gw.converse(NOVA_MODEL_ID, request_data)
    assert gw.client.calls[0]['modelId'] == 'global.amazon.nova-2-lite-v1:0'
    record, = records(tmp_path)
    assert record['decision'] == 'allow'
    assert Decimal(record['reserved_cost']) == Decimal('0.00028')
    assert gw.budget.cost == Decimal('0.000055')
    assert Decimal(record['cost']) == gw.budget.cost


def test_nova_regional_profile_not_implicitly_allowed(tmp_path, request_data):
    gw = gateway(tmp_path)
    with pytest.raises(GatewayBlocked, match='model_not_allowed'):
        gw.converse('us.amazon.nova-2-lite-v1:0', request_data)
    assert not gw.client.calls
