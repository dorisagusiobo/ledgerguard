"""No live calls: canned data is selected by the exact bytes sent to the client."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path

import boto3
import pytest

from ledgerguard.extraction_eval import compare, main, render
from ledgerguard.extractor import build_request
from ledgerguard.gateway import DEFAULT_PRICES, MODEL_ID, NOVA_MODEL_ID, estimate_input


class FakeClient:
    def __init__(self, data, mutate=None):
        self.records = {}
        for path in (data / 'invoices').glob('*.json'):
            pdf = (data / 'invoices_pdf' / f'{path.stem}.pdf').read_bytes()
            self.records[pdf] = json.loads(path.read_text())
        self.calls, self.mutate = [], mutate

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        pdf = kwargs['messages'][0]['content'][0]['document']['source']['bytes']
        record = deepcopy(self.records[pdf])
        if self.mutate:
            self.mutate(record)
        return {'stopReason': 'tool_use', 'usage': {'inputTokens': 1000, 'outputTokens': 1000},
                'output': {'message': {'content': [{'toolUse': {'name': 'record_invoice', 'input': record}}]}}}


def run(data, tmp_path, *, client=None, **kwargs):
    return compare(data, client=client or FakeClient(data), max_cost=kwargs.pop('max_cost', Decimal('10')),
                   audit_path=tmp_path / 'audit.jsonl', **kwargs)


def test_no_live_never_creates_client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('boto3 client must not be created without --live')
    monkeypatch.setattr(boto3, 'client', forbidden)
    with pytest.raises(SystemExit) as error:
        main(['--data', '/nonexistent', '--max-cost', '1.00'])
    assert error.value.code == 2


def test_max_cost_is_required(monkeypatch):
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('must not create client'))
    with pytest.raises(SystemExit):
        main(['--data', '/nonexistent', '--live'])


def test_perfect_fake_extraction_matches_dev_baseline(generated, tmp_path):
    data = generated(7)
    client = FakeClient(data)
    report = run(data, tmp_path, client=client, models=('sonnet', 'nova'))
    assert list(report['models']) == ['nova', 'sonnet']
    baseline = report['baseline']
    assert baseline['overall']['caught'] == 12
    assert baseline['overall']['expected'] == 14
    assert baseline['overall']['precision'] == 1
    assert baseline['clean_invoices'] == {'wrongly_flagged': 0, 'total': 20}
    for model in report['models'].values():
        assert model['attempted'] == model['successful'] == 38
        assert all(value == 1 for value in model['field_accuracy'].values())
        assert model['line_items_accuracy'] == model['source_accuracy'] == model['whole_invoice_accuracy'] == 1
        assert model['error_preservation'] == {'expected': 2, 'preserved': 2, 'accuracy': 1}
        assert model['mismatches'] == []
        assert model['downstream'] == baseline
        assert model['tokens'] == {'input': 38000, 'output': 38000}
        assert model['timing']['calls'] == 38
        assert model['timing']['slowest_seconds'] >= model['timing']['median_seconds'] >= 0
    assert client.calls[0]['modelId'] == NOVA_MODEL_ID
    assert client.calls[-1]['modelId'] == MODEL_ID
    # Only PDF bytes, schema and fixed prompts are exposed to the client.
    for request in client.calls:
        assert set(request) == {'modelId', 'system', 'messages', 'toolConfig', 'inferenceConfig'}
        assert len(request['messages'][0]['content']) == 2
    assert report['metadata']['seed'] == 7 and report['metadata']['git_revision']


def test_one_wrong_header_is_one_safe_mismatch(generated, tmp_path):
    data = generated(7)
    number = sorted(n for n, s in json.loads((data / 'splits.json').read_text()).items() if s == 'dev')[0]
    secret = 'PRIVATE-CHANGED-VENDOR'

    def mutate(record):
        if record['invoice_number'] == number:
            record['vendor_name'] = secret

    report = run(data, tmp_path, client=FakeClient(data, mutate), models=('nova',))
    model = report['models']['nova']
    assert model['mismatches'] == [{'invoice_number': number, 'field': 'vendor_name'}]
    assert model['field_accuracy']['vendor_name'] == 37 / 38
    assert secret not in json.dumps(report)
    assert secret not in render(report)


def test_fixed_planted_total_fails_error_preservation(generated, tmp_path):
    data = generated(7)

    def fix(record):
        record['total'] = str(Decimal(record['subtotal']) + Decimal(record['tax']))

    report = run(data, tmp_path, client=FakeClient(data, fix), models=('nova',))
    assert report['models']['nova']['error_preservation'] == {'expected': 2, 'preserved': 0, 'accuracy': 0}
    assert report['models']['nova']['downstream']['by_type']['arithmetic_error']['caught'] == 0


def test_budget_stop_is_partial_and_stops_all_models(generated, tmp_path):
    data = generated(7)
    ids = sorted(n for n, s in json.loads((data / 'splits.json').read_text()).items() if s == 'dev')
    pdf = (data / 'invoices_pdf' / f'{ids[0]}.pdf').read_bytes()
    reserved = DEFAULT_PRICES[NOVA_MODEL_ID].cost(estimate_input(build_request(pdf, NOVA_MODEL_ID)), 2000)
    client = FakeClient(data)
    report = run(data, tmp_path, client=client, limit=2, max_cost=reserved + Decimal('0.0014'))
    assert report['stopped'] == 'max_cost_exceeded'
    assert len(client.calls) == 1
    assert report['models']['nova']['attempted'] == 1
    assert report['models']['sonnet']['attempted'] == 0
    assert len(report['models']['sonnet']['not_attempted']) == 2
    audit = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert [r['decision'] for r in audit] == ['allow', 'block']
    assert report['batch']['calls'] == 1


def test_cost_limit_is_shared_across_models(generated, tmp_path):
    data = generated(7)
    ids = sorted(n for n, s in json.loads((data / 'splits.json').read_text()).items() if s == 'dev')
    pdf = (data / 'invoices_pdf' / f'{ids[0]}.pdf').read_bytes()
    sonnet_reserve = DEFAULT_PRICES[MODEL_ID].cost(estimate_input(build_request(pdf, MODEL_ID)), 2000)
    client = FakeClient(data)
    limit = sonnet_reserve + Decimal('0.0056') + Decimal('0.009')
    report = run(data, tmp_path, client=client, limit=2, max_cost=limit)
    assert report['stopped'] == 'max_cost_exceeded'
    assert [c['modelId'] for c in client.calls] == [NOVA_MODEL_ID, NOVA_MODEL_ID, MODEL_ID]
    assert Decimal(report['batch']['cost']) == Decimal('0.0236')
    assert Decimal(report['batch']['cost']) == sum(Decimal(m['cost']['total']) for m in report['models'].values())


def test_referrals_use_trusted_document_identity(generated, tmp_path):
    data = generated(7)
    def invalid(record):
        record['total'] = 0.1
    report = run(data, tmp_path, client=FakeClient(data, invalid), models=('nova',), limit=2)
    model = report['models']['nova']
    assert model['referrals']['count'] == 2
    assert model['referrals']['by_reason'] == {'extraction validation failed at loc': 2}
    assert len({r['invoice_number'] for r in model['referrals']['invoices']}) == 2
    assert model['whole_invoice_accuracy'] == 0


def test_incorrect_extracted_number_does_not_break_downstream(generated, tmp_path):
    data = generated(7)
    report = run(data, tmp_path, client=FakeClient(data, lambda r: r.update(invoice_number='WRONG-NUMBER')),
                 models=('nova',), limit=2)
    model = report['models']['nova']
    assert model['field_accuracy']['invoice_number'] == 0
    assert model['downstream']['overall']['caught'] == 1
    assert all(m['field'] == 'invoice_number' for m in model['mismatches'])
    assert 'WRONG-NUMBER' not in json.dumps(report)


def test_audit_io_failure_propagates(generated, tmp_path):
    data = generated(7)
    (tmp_path / 'audit.jsonl').mkdir()
    client = FakeClient(data)
    with pytest.raises(OSError):
        run(data, tmp_path, client=client, models=('nova',), limit=1)
    assert not client.calls


@pytest.mark.parametrize('max_cost,stopped,calls', [('1.00', None, 4), ('0.000001', 'max_cost_exceeded', 0)])
def test_live_cli_with_fake_client_saves_report_and_configures_no_retries(generated, tmp_path, monkeypatch, capsys, max_cost, stopped, calls):
    data = generated(7)
    client = FakeClient(data)
    def factory(service, **kwargs):
        assert service == 'bedrock-runtime'
        assert kwargs['region_name'] == 'us-east-1'
        assert kwargs['config'].retries == {'total_max_attempts': 1}
        return client
    monkeypatch.setattr(boto3, 'client', factory)
    output = tmp_path / 'report.json'
    main(['--data', str(data), '--models', 'sonnet,nova', '--max-cost', max_cost,
          '--limit', '2', '--live', '--json', str(output), '--audit', str(tmp_path / 'audit.jsonl')])
    report = json.loads(output.read_text())
    assert report['metadata']['max_cost'] == max_cost
    assert report['batch']['calls'] == calls
    assert report['stopped'] == stopped
    assert list(report['models']) == ['nova', 'sonnet']
    assert 'truth baseline' in capsys.readouterr().out
