"""Budgeted Bedrock Converse boundary. No SDK client is created by this module.

Prices: https://aws.amazon.com/bedrock/pricing/ (global standard inference),
verified 2026-10-29. AWS page token data:
https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrockfoundationmodels/USD/current/bedrockfoundationmodels.json
Sonnet input key y4HVRmBnwVLc-QbdvU2n3x_K3p1HLrk6Al35zDDvLyw = 3;
output key nRfCau6wD_BMENoXX3ua8g2QrVfHLN4z6R5-qKNnq0g = 15 USD/1M.

Nova 2 Lite global standard pricing uses the page's bedrock/bedrock dataset:
https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrock/USD/current/bedrock.json
Input key NB7aHsk8SCFhgsXJJRvBTxu4xhoyw2_4c97GBxqcHrQ = 0.0003;
output key pChkwb9lpAj18OhnQBD7c2I4YO2hSqm451bQtxrTfvI = 0.0025.
The page multiplies these per-1K rates by 1000: 0.30 / 2.50 USD/1M.

Invoice/batch budgets are shared in memory; an optional MonthlyLedger persists
monthly reservations. Reuse the run budget across every pipeline stage.
Disable SDK automatic retries so each network attempt passes this boundary.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from pydantic import ValidationError

from ledgerguard.audit import AuditSink, JsonlSink
from ledgerguard.tools import MAX_ROWS, TOOL_ARGUMENTS

MILLION = Decimal('1000000')
MODEL_ID = 'global.anthropic.claude-sonnet-4-6'
NOVA_MODEL_ID = 'global.amazon.nova-2-lite-v1:0'


class GatewayBlocked(RuntimeError):
    """Rejected before invocation; error messages never contain request content."""

    def __init__(self, reason: str, *, scope: str | None = None):
        super().__init__(reason)
        self.scope = scope


def decimal_value(value):
    if isinstance(value, (float, bool)):
        raise ValueError('Costs must use Decimal or decimal strings, never float')
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError('Costs must be finite and nonnegative')
    return result


def token_count(value):
    if type(value) is not int or value < 0:
        raise ValueError('Token counts must be nonnegative integers')
    return value


@dataclass(frozen=True)
class Prices:
    input_per_million: Decimal
    output_per_million: Decimal

    def __post_init__(self):
        object.__setattr__(self, 'input_per_million', decimal_value(self.input_per_million))
        object.__setattr__(self, 'output_per_million', decimal_value(self.output_per_million))

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        return (token_count(input_tokens) * self.input_per_million
                + token_count(output_tokens) * self.output_per_million) / MILLION


DEFAULT_PRICES = {
    MODEL_ID: Prices(Decimal('3'), Decimal('15')),
    NOVA_MODEL_ID: Prices(Decimal('0.30'), Decimal('2.50')),
}


@dataclass
class Budget:
    max_calls: int = 20
    max_cost: Decimal = Decimal('0.50')
    max_tool_calls: int = 10
    label: str | None = None
    calls: int = field(default=0, init=False)
    tool_calls: int = field(default=0, init=False)
    cost: Decimal = field(default=Decimal(0), init=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False, compare=False)

    def __post_init__(self):
        token_count(self.max_calls)
        token_count(self.max_tool_calls)
        self.max_cost = decimal_value(self.max_cost)


def estimate_input(request: dict) -> int:
    """Local estimate: one token per serialized byte plus framing overhead.

    Binary documents are counted by their byte length. This is an estimate, not
    an exact tokenizer bound; production callers can inject a better estimator.
    If reported usage exceeds the reservation, actual cost is still charged.
    """
    def size(value):
        if isinstance(value, bytes):
            return len(value)
        if isinstance(value, dict):
            return sum(len(str(k).encode()) + size(v) for k, v in value.items())
        if isinstance(value, (list, tuple)):
            return sum(size(v) for v in value)
        return len(str(value).encode('utf-8'))
    return size(request) + 1024


def contains_cache_point(value):
    if isinstance(value, dict):
        return 'cachePoint' in value or any(contains_cache_point(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_cache_point(v) for v in value)
    return False


class Gateway:
    def __init__(self, *, client, budget: Budget, run_id: str, audit_path: Path | None = None,
                 audit: AuditSink | None = None, monthly=None,
                 prices: dict[str, Prices] | None = None,
                 input_estimator: Callable[[dict], int] = estimate_input,
                 shared_budgets: tuple[Budget, ...] = ()):
        self.client, self.budget, self.run_id = client, budget, run_id
        self.prices = dict(DEFAULT_PRICES if prices is None else prices)
        self.input_estimator = input_estimator
        self.budgets = list({id(b): b for b in (budget, *shared_budgets)}.values())
        if (audit is None) == (audit_path is None):
            raise ValueError('Provide exactly one of audit or audit_path')
        self.audit = audit if audit is not None else JsonlSink(audit_path)
        self.audit_path = self.audit.path if isinstance(self.audit, JsonlSink) else None
        self.monthly = monthly
        # SDK retries would bypass per-attempt accounting. Never alter the client.
        config = getattr(getattr(client, 'meta', None), 'config', None)
        retries = getattr(config, 'retries', None) or {}
        if retries and retries.get('total_max_attempts') != 1 and retries.get('max_attempts') != 0:
            raise ValueError('Configure the Bedrock client with total_max_attempts=1')

    def converse(self, model_id: str, request: dict):
        # Hold shared-budget locks through reconciliation: no overlapping calls
        # can spend the same reservation. Lock order is consistent across gateways.
        with ExitStack() as stack:
            for budget in sorted(self.budgets, key=id):
                stack.enter_context(budget._lock)
            # Open before invocation: an unwritable audit destination prevents calls.
            writer = stack.enter_context(self.audit.session())
            record = {'time': datetime.now(timezone.utc).isoformat(), 'model': model_id,
                      'run_id': self.run_id, 'decision': 'block', 'outcome': 'blocked',
                      'reason': None, 'input_tokens': 0, 'output_tokens': 0,
                      'cost': '0', 'reserved_cost': '0'}

            def audit():
                writer.append(record)

            reservation = None

            def release():
                nonlocal reservation
                if reservation is not None:
                    self.monthly.reconcile(reservation, Decimal(0))
                    reservation = None

            def block(reason, scope=None):
                release()
                record['reason'] = reason
                record['scope'] = scope
                audit()
                raise GatewayBlocked(reason, scope=scope)

            if model_id not in self.prices:
                block('model_not_allowed')
            request = deepcopy(request)
            try:
                max_tokens = request['inferenceConfig']['maxTokens']
                token_count(max_tokens)
                if max_tokens == 0 or 'modelId' in request:
                    raise ValueError()
                # Only the verified standard, noncached price path is supported.
                if contains_cache_point(request) or 'performanceConfig' in request or 'serviceTier' in request:
                    raise ValueError()
                estimate = token_count(self.input_estimator(request))
                reserved = self.prices[model_id].cost(estimate, max_tokens)
            except (KeyError, TypeError, ValueError):
                block('invalid_or_unsupported_request')
            if self.monthly is not None:
                try:
                    reservation = self.monthly.reserve(reserved)
                except GatewayBlocked as error:
                    block(str(error), error.scope)
            for budget in self.budgets:
                if budget.calls + 1 > budget.max_calls:
                    block('max_calls_exceeded', budget.label)
                if budget.cost + reserved > budget.max_cost:
                    block('max_cost_exceeded', budget.label)
            record.update(decision='allow', reserved_cost=str(reserved))
            try:
                writer.authorize(record)
            except BaseException:
                release()
                raise
            for budget in self.budgets:
                budget.calls += 1
                budget.cost += reserved
            try:
                response = self.client.converse(modelId=model_id, **request)
            except Exception:
                for budget in self.budgets:
                    budget.cost -= reserved
                release()
                record.update(outcome='client_error', reason='client_error')
                audit()
                # Do not propagate provider exception text, which can echo input.
                raise RuntimeError('Bedrock call failed; reservation released') from None
            try:
                usage = response['usage']
                inputs = token_count(usage['inputTokens'])
                outputs = token_count(usage['outputTokens'])
                if usage.get('cacheReadInputTokens', 0) or usage.get('cacheWriteInputTokens', 0):
                    raise ValueError()
                actual = self.prices[model_id].cost(inputs, outputs)
            except (KeyError, TypeError, ValueError):
                if reservation is not None:
                    self.monthly.reconcile(reservation, reserved)
                # Unknown usage must not silently release potentially spent funds.
                record.update(outcome='invalid_usage', reason='reservation_retained', cost=str(reserved))
                audit()
                raise RuntimeError('Bedrock usage invalid; reservation retained') from None
            if reservation is not None:
                self.monthly.reconcile(reservation, actual)
            for budget in self.budgets:
                budget.cost += actual - reserved
            record.update(outcome='success', input_tokens=inputs, output_tokens=outputs, cost=str(actual))
            if actual > reserved:
                record['reason'] = 'usage_exceeded_estimate'
            audit()
            return response


    def call_tool(self, name: str, arguments: dict, tools):
        """Authorize read-only tools before execution, then append completion.

        Every attempt consumes a slot, even invalid or over-cap attempts. Two
        records for allowed calls preserve append-only pre-execution authorization
        and final outcomes; blocked calls need only one record. Values never log.
        """
        with ExitStack() as stack:
            for budget in sorted(self.budgets, key=id):
                stack.enter_context(budget._lock)
            writer = stack.enter_context(self.audit.session())
            for budget in self.budgets:
                budget.tool_calls += 1
            record = {'time': datetime.now(timezone.utc).isoformat(), 'run_id': self.run_id,
                      'call_id': uuid4().hex, 'tool': name, 'phase': 'authorization',
                      'decision': 'block', 'reason': None, 'outcome': 'blocked', 'row_count': 0}

            def audit():
                writer.append(record)

            def block(reason, scope=None):
                record['reason'] = reason
                record['scope'] = scope
                audit()
                raise GatewayBlocked(reason, scope=scope)

            if not isinstance(name, str) or name not in TOOL_ARGUMENTS:
                block('tool_not_allowed')
            try:
                args = TOOL_ARGUMENTS[name].model_validate(arguments)
            except (ValidationError, TypeError, ValueError):
                block('invalid_tool_arguments')
            for budget in self.budgets:
                if budget.tool_calls > budget.max_tool_calls:
                    block('max_tool_calls_exceeded', budget.label)
            record.update(decision='allow', outcome='authorized', row_count=None)
            audit()  # Failure here stops execution; nothing has run yet.
            try:
                result = getattr(tools, name)(**args.model_dump())
                if (not isinstance(result, dict) or not isinstance(result.get('rows'), list)
                        or len(result['rows']) > MAX_ROWS or type(result.get('truncated')) is not bool):
                    raise ValueError('Invalid tool result shape')
            except Exception:
                record.update(time=datetime.now(timezone.utc).isoformat(), phase='completion',
                              outcome='tool_error', reason='tool_error', row_count=None)
                audit()  # Audit I/O failures propagate unchanged and stop the run.
                raise RuntimeError('Read-only tool failed') from None
            record.update(time=datetime.now(timezone.utc).isoformat(), phase='completion',
                          outcome='success', row_count=len(result['rows']))
            audit()
            return result
