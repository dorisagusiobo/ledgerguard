"""Policy eval fakes choose from request descriptions, never from answer keys."""
from decimal import Decimal
import json

import boto3
import pytest

from ledgerguard.gateway import DEFAULT_PRICES, MODEL_ID, NOVA_MODEL_ID, estimate_input
from ledgerguard.policy import AllClauses, build_request
from ledgerguard.policy_eval import compare, main, render
from ledgerguard.rules import load_inputs

SEMANTIC = {'Reloadable spending card for staff', 'Store credit - staff rewards',
            'Prepaid Visa card', 'Holiday gift certificates'}


class FakeClient:
    def __init__(self, flag=SEMANTIC, confidence=0.9):
        self.flag, self.confidence, self.calls = set(flag), confidence, []

    def converse(self, **request):
        self.calls.append(request)
        payload = json.loads(request['messages'][0]['content'][0]['text'])
        flags = [{'clause_id': 'POL-4', 'line_index': item['index'], 'confidence': self.confidence}
                 for item in payload['line_items'] if item['index'] == 0 and item['description'] in self.flag]
        return {'stopReason': 'tool_use', 'usage': {'inputTokens': 1000, 'outputTokens': 100},
                'output': {'message': {'content': [{'toolUse': {
                    'name': 'record_policy_findings', 'input': {'violations': flags}}}]}}}


def run(data, tmp_path, **kwargs):
    return compare(data, client=kwargs.pop('client', FakeClient()),
                   max_cost=kwargs.pop('max_cost', Decimal('1.00')),
                   audit_path=tmp_path / 'audit.jsonl', **kwargs)


def reservation(data, model):
    inputs = load_inputs(data)
    splits = json.loads((data / 'splits.json').read_text())
    invoice = sorted((i for i in inputs.invoices if splits[i.invoice_number] == 'dev'),
                     key=lambda i: i.invoice_number)[0]
    clauses = AllClauses(inputs.policy_clauses).retrieve(invoice)
    return DEFAULT_PRICES[model].cost(estimate_input(build_request(invoice, clauses)), 1000)


def test_without_live_or_without_budget_never_creates_client(monkeypatch):
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('AWS client must not be created'))
    for argv in [['--data', '/missing', '--max-cost', '1'], ['--data', '/missing', '--live']]:
        with pytest.raises(SystemExit) as error:
            main(argv)
        assert error.value.code == 2


def test_perfect_fake_reaches_14_and_rules_run_once(generated, tmp_path, monkeypatch):
    import ledgerguard.policy_eval as module
    original, calls = module.run_rules, []

    def rules_once(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, 'run_rules', rules_once)
    client = FakeClient()
    report = run(generated(7), tmp_path, client=client, models=('sonnet', 'nova'))
    assert len(calls) == 1
    assert list(report['models']) == ['nova', 'sonnet']
    assert report['baseline']['overall']['caught'] == 12
    assert report['baseline']['overall']['expected'] == 14
    assert report['baseline']['clean_invoices'] == {'wrongly_flagged': 0, 'total': 20}
    for model in report['models'].values():
        overall = model['rules_plus_policy']['overall']
        assert overall['caught'] == overall['expected'] == 14
        assert overall['recall'] == overall['precision'] == 1
        assert overall['false_flags'] == 0
        assert model['policy_violation']['caught'] == model['policy_violation']['expected'] == 4
        assert model['rules_only'] == report['baseline']
        assert model['attempted'] == 38 and len(model['policy_flags']) == 2
        assert all(set(f) == {'invoice_number', 'clause_id', 'confidence'} for f in model['policy_flags'])
        assert not ({'INV-2026-0006', 'INV-2026-0007'} & {f['invoice_number'] for f in model['policy_flags']})
        assert model['tokens'] == {'input': 38000, 'output': 3800}
        assert model['timing']['calls'] == 38
    assert [r['modelId'] for r in client.calls] == [NOVA_MODEL_ID] * 38 + [MODEL_ID] * 38
    assert 'weak evidence' in report['limitation']


def test_lookalike_is_listed_and_reduces_precision(generated, tmp_path):
    report = run(generated(7), tmp_path, client=FakeClient(SEMANTIC | {'Gift wrap paper'}), models=('nova',))
    model = report['models']['nova']
    assert model['rules_plus_policy']['overall']['recall'] == 1
    assert model['rules_plus_policy']['overall']['precision'] == 14 / 15
    assert [f['invoice_numbers'] for f in model['false_flags']] == [['INV-2026-0007']]
    assert model['rules_plus_policy']['clean_invoices'] == {'wrongly_flagged': 1, 'total': 20}
    assert 'False flags: INV-2026-0007' in render(report)


def test_low_confidence_is_referral_not_caught(generated, tmp_path):
    report = run(generated(7), tmp_path, client=FakeClient(confidence=0.4), models=('nova',))
    model = report['models']['nova']
    assert model['policy_flags'] == []
    assert model['referrals']['count'] == 2
    assert model['referrals']['by_reason'] == {'policy uncertain': 2}
    assert model['rules_plus_policy']['overall']['caught'] == 12


def test_request_excludes_code_clauses_answers_and_pdfs(generated, tmp_path):
    client = FakeClient()
    run(generated(7), tmp_path, client=client, models=('nova',), limit=2)
    for request in client.calls:
        encoded = json.dumps(request)  # Would fail if PDF bytes were included.
        for forbidden in ['POL-1', 'POL-2', 'POL-3', 'answer_key', 'amount_at_issue', 'expected_findings']:
            assert forbidden not in encoded
        payload = json.loads(request['messages'][0]['content'][0]['text'])
        assert set(payload) == {'line_items', 'clauses'}
        assert all(set(i) == {'index', 'description', 'quantity', 'amount'} for i in payload['line_items'])


def test_budget_stop_is_partial_and_stops_all_later_models(generated, tmp_path):
    data = generated(7)
    client = FakeClient()
    report = run(data, tmp_path, client=client, limit=2,
                 max_cost=reservation(data, NOVA_MODEL_ID) + Decimal('0.000275'))
    assert report['stopped'] == 'max_cost_exceeded'
    assert len(client.calls) == 1
    assert report['models']['nova']['attempted'] == 1
    assert report['models']['sonnet']['attempted'] == 0
    assert report['models']['sonnet']['rules_plus_policy'] == report['baseline']
    audit = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert [r['decision'] for r in audit] == ['allow', 'block']


def test_one_shared_budget_across_models(generated, tmp_path):
    data = generated(7)
    client = FakeClient()
    maximum = reservation(data, MODEL_ID) + Decimal('0.0011') + Decimal('0.00225')
    report = run(data, tmp_path, client=client, limit=2, max_cost=maximum)
    assert report['stopped'] == 'max_cost_exceeded'
    assert [r['modelId'] for r in client.calls] == [NOVA_MODEL_ID, NOVA_MODEL_ID, MODEL_ID]
    assert Decimal(report['batch']['cost']) == Decimal('0.0056')
    assert Decimal(report['batch']['cost']) == sum(Decimal(m['cost']['total']) for m in report['models'].values())


@pytest.mark.parametrize('maximum,stopped,count', [('1.00', None, 4), ('0.000001', 'max_cost_exceeded', 0)])
def test_cli_json_partial_output_and_retry_config(generated, tmp_path, monkeypatch, capsys, maximum, stopped, count):
    client = FakeClient()
    def factory(service, **kwargs):
        assert service == 'bedrock-runtime' and kwargs['region_name'] == 'us-east-1'
        assert kwargs['config'].retries == {'total_max_attempts': 1}
        return client
    monkeypatch.setattr(boto3, 'client', factory)
    output = tmp_path / 'report.json'
    main(['--data', str(generated(7)), '--max-cost', maximum, '--limit', '2', '--live',
          '--json', str(output), '--audit', str(tmp_path / 'audit.jsonl')])
    report = json.loads(output.read_text())
    assert report['stopped'] == stopped and report['batch']['calls'] == count
    assert report['metadata']['max_cost'] == maximum
    assert report['metadata']['min_confidence'] == 0.5
    assert report['metadata']['seed'] == 7 and report['metadata']['git_revision']
    assert 'rules only' in capsys.readouterr().out


@pytest.mark.parametrize('unsafe', ['inputs', 'audit', 'data_audit'])
def test_unsafe_paths_rejected_before_client(generated, tmp_path, monkeypatch, unsafe):
    data = generated(7)
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('unsafe paths must fail before client creation'))
    audit = tmp_path / 'audit.jsonl'
    output = tmp_path / 'report.json'
    if unsafe == 'inputs':
        output = data / 'report.json'
    elif unsafe == 'audit':
        output = audit
    else:
        audit = data / 'audit.jsonl'
    with pytest.raises(SystemExit):
        main(['--data', str(data), '--max-cost', '1', '--live', '--json', str(output), '--audit', str(audit)])


def test_audit_failure_stops_without_client_call(generated, tmp_path):
    (tmp_path / 'audit.jsonl').mkdir()
    client = FakeClient()
    with pytest.raises(OSError):
        run(generated(7), tmp_path, client=client, models=('nova',), limit=1)
    assert not client.calls
