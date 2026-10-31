"""Scripted investigator evaluation; no live calls or evaluation-answer access."""
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

import boto3
import pytest

from ledgerguard.gateway import DEFAULT_PRICES, MODEL_ID, NOVA_MODEL_ID, estimate_input
from ledgerguard.investigator import SYSTEM_PROMPT, initial_message, tool_specs
from ledgerguard.investigator_eval import compare, render
from ledgerguard.rules import load_inputs, run_rules


class ScriptedClient:
    def __init__(self, wrong_duplicate=False, summary='Context checked against the supplied records.', never_stop=False):
        self.calls, self.wrong_duplicate = [], wrong_duplicate
        self.summary, self.never_stop = summary, never_stop

    def converse(self, **request):
        self.calls.append(request)
        payload = json.loads(request['messages'][0]['content'][0]['text'])
        invoice, findings = payload['invoice'], payload['findings']
        tool_names = []
        kinds = {f['type'] for f in findings}
        if 'duplicate' in kinds:
            tool_names.append('vendor_history' if self.wrong_duplicate else 'duplicate_search')
        if 'split_purchase' in kinds:
            tool_names.append('related_invoices')
        if 'unusual_amount' in kinds:
            tool_names.append('vendor_history')
        if self.never_stop:
            tool_names = ['vendor_history']
        if tool_names and (len(request['messages']) == 1 or self.never_stop):
            uses = []
            for i, name in enumerate(sorted(set(tool_names))):
                args = {'vendor_name': invoice['vendor_name']}
                if name == 'duplicate_search':
                    args = {'invoice_number': invoice['invoice_number']}
                if name == 'related_invoices':
                    start = date.fromisoformat(invoice['invoice_date'])
                    args.update(start=start.isoformat(), end=(start + timedelta(days=7)).isoformat())
                uses.append({'toolUse': {'toolUseId': f'USE-{len(self.calls)}-{i}', 'name': name, 'input': args}})
        else:
            numbers = {n for f in findings for n in f['invoice_numbers']}
            uses = [{'toolUse': {'toolUseId': f'SUBMIT-{len(self.calls)}', 'name': 'submit_notes', 'input': {
                'notes': [{'finding_index': f['index'], 'summary': self.summary,
                           'related_invoices': sorted(numbers), 'uncertainty': 'medium'} for f in findings]}}}]
        return {'stopReason': 'tool_use', 'usage': {'inputTokens': 1000, 'outputTokens': 100},
                'output': {'message': {'role': 'assistant', 'content': uses}}}


def run(data, tmp_path, **kwargs):
    return compare(data, client=kwargs.pop('client', ScriptedClient()),
                   max_cost=kwargs.pop('max_cost', Decimal('1.00')),
                   audit_path=tmp_path / 'audit.jsonl', **kwargs)


def initial_reservation(data, model):
    inputs = load_inputs(data)
    splits = json.loads((data / 'splits.json').read_text())
    invoices = [i for i in inputs.invoices if splits[i.invoice_number] == 'dev']
    findings = run_rules(invoices, inputs.contracts, inputs.history, inputs.policy_clauses).findings
    number = sorted({f.invoice_numbers[0] for f in findings})[0]
    group = [f for f in findings if f.invoice_numbers[0] == number]
    invoice = next(i for i in invoices if i.invoice_number == number)
    request = {'system': [{'text': SYSTEM_PROMPT}], 'messages': [initial_message(invoice, group)],
               'toolConfig': {'tools': tool_specs(), 'toolChoice': {'any': {}}},
               'inferenceConfig': {'maxTokens': 2000, 'temperature': 0}}
    return DEFAULT_PRICES[model].cost(estimate_input(request), 2000)


def test_normal_completion_tools_unchanged_and_batch_caps(generated, tmp_path):
    client = ScriptedClient()
    report = run(generated(7), tmp_path, client=client, models=('sonnet', 'nova'))
    assert list(report['models']) == ['nova', 'sonnet']
    assert len(report['selected_groups']) == 12 and report['stopped'] is None
    for model in report['models'].values():
        assert model['completed'] == model['attempted'] == 12
        assert model['completion_rate'] == 1
        assert model['findings_unchanged']['rate'] == 1
        assert model['referrals']['count'] == 0
        assert all(row == {'expected': 2, 'used': 2, 'rate': 1} for row in model['expected_tool_used'].values())
        assert len(model['notes']) == 12
        assert model['fraud_mentions']['count'] == 0
        assert model['turns']['max'] == 2 and model['tool_calls']['max'] == 1
        assert model['tools_executed'] == {'duplicate_search': 2, 'related_invoices': 2, 'vendor_history': 2}
        assert all(counts == {'low': 0, 'medium': 2, 'high': 0} for counts in model['uncertainty'].values())
    assert report['batch']['max_calls'] == 12 * 2 * 6
    assert report['batch']['max_tool_calls'] == 12 * 2 * 10
    assert report['batch']['tool_calls'] == 12  # Would exceed the old shared default of ten.
    assert client.calls[0]['modelId'] == NOVA_MODEL_ID
    assert client.calls[-1]['modelId'] == MODEL_ID
    assert report['metadata']['seed'] == 7 and report['metadata']['git_revision']


def test_wrong_tool_lowers_duplicate_rate(generated, tmp_path):
    report = run(generated(7), tmp_path, client=ScriptedClient(wrong_duplicate=True), models=('nova',))
    model = report['models']['nova']
    assert model['completed'] == 12
    assert model['expected_tool_used']['duplicate'] == {'expected': 2, 'used': 0, 'rate': 0}
    assert model['expected_tool_used']['split_purchase']['rate'] == 1


def test_fraud_substring_is_flag_for_manual_review_not_failure(generated, tmp_path):
    summary = 'This is not evidence of FRAUD.'
    report = run(generated(7), tmp_path, client=ScriptedClient(summary=summary), models=('nova',), limit=2)
    model = report['models']['nova']
    assert model['completed'] == 2 and model['fraud_mentions']['count'] == 2
    assert model['fraud_mentions']['invoice_numbers'] == report['selected_groups']
    assert all(note['summary'] == summary for note in model['notes'])
    assert summary not in render(report)


def test_never_stopping_model_hits_turn_cap(generated, tmp_path):
    client = ScriptedClient(never_stop=True)
    report = run(generated(7), tmp_path, client=client, models=('nova',), max_turns=6)
    model = report['models']['nova']
    assert model['completed'] == 0 and model['attempted'] == 12
    assert model['referrals']['by_reason'] == {'turn cap': 12}
    assert model['turns'] == {'median': 6, 'max': 6}
    assert len(client.calls) == 72
    for offset in range(0, 72, 6):
        assert all(r['toolConfig']['toolChoice'] == {'any': {}} for r in client.calls[offset:offset + 5])
        assert client.calls[offset + 5]['toolConfig']['toolChoice'] == {'tool': {'name': 'submit_notes'}}
    assert model['findings_unchanged']['rate'] == 1


def test_answer_key_is_never_opened(generated, tmp_path, monkeypatch):
    data = generated(7)
    original = Path.open

    def checked(path, *args, **kwargs):
        assert 'answer_key' not in path.parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', checked)
    report = run(data, tmp_path, models=('nova',))
    assert report['models']['nova']['completed'] == 12


def test_cost_stop_keeps_paid_partial_investigation(generated, tmp_path):
    data, client = generated(7), ScriptedClient()
    maximum = initial_reservation(data, NOVA_MODEL_ID) + Decimal('0.000275')
    report = run(data, tmp_path, client=client, max_cost=maximum, limit=2)
    assert report['stopped'] == 'max_cost_exceeded'
    assert len(client.calls) == 1
    first, second = report['models']['nova'], report['models']['sonnet']
    assert first['attempted'] == 1 and first['completed'] == 0
    assert first['referrals']['by_reason'] == {'batch cost limit': 1}
    assert second['attempted'] == 0
    assert Decimal(first['cost']['total']) == Decimal(report['batch']['cost']) == Decimal('0.00055')
    assert first['timing']['calls'] == 1 and first['tool_calls']['max'] == 1


def test_batch_shared_across_models_and_partial_sonnet_cost_retained(generated, tmp_path):
    data, client = generated(7), ScriptedClient()
    maximum = initial_reservation(data, MODEL_ID) + Decimal('0.0011') + Decimal('0.00225')
    report = run(data, tmp_path, client=client, max_cost=maximum, limit=1)
    assert report['stopped'] == 'max_cost_exceeded'
    assert [r['modelId'] for r in client.calls] == [NOVA_MODEL_ID, NOVA_MODEL_ID, MODEL_ID]
    assert report['models']['nova']['completed'] == 1
    assert report['models']['sonnet']['completed'] == 0
    assert report['models']['sonnet']['attempted'] == 1
    assert Decimal(report['batch']['cost']) == Decimal('0.0056')
    assert Decimal(report['batch']['cost']) == sum(Decimal(m['cost']['total']) for m in report['models'].values())


def test_no_live_or_missing_cost_cannot_create_client(monkeypatch):
    from ledgerguard.investigator_eval import main
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('must not create client'))
    for argv in [['--data', '/missing', '--max-cost', '1'], ['--data', '/missing', '--live']]:
        with pytest.raises(SystemExit):
            main(argv)


@pytest.mark.parametrize('maximum,stopped', [('1.00', None), ('0.000001', 'max_cost_exceeded')])
def test_cli_fake_saves_full_and_partial_reports(generated, tmp_path, monkeypatch, capsys, maximum, stopped):
    from ledgerguard.investigator_eval import main
    client = ScriptedClient(summary='Manual reviewer summary.')

    def factory(service, **kwargs):
        assert service == 'bedrock-runtime' and kwargs['region_name'] == 'us-east-1'
        assert kwargs['config'].retries == {'total_max_attempts': 1}
        return client

    monkeypatch.setattr(boto3, 'client', factory)
    monkeypatch.chdir(tmp_path)
    output = tmp_path / 'report.json'
    main(['--data', str(generated(7)), '--max-cost', maximum, '--limit', '2', '--live', '--json', str(output)])
    report = json.loads(output.read_text())
    assert report['stopped'] == stopped and report['metadata']['max_cost'] == maximum
    assert list((tmp_path / 'runs').glob('investigator-*.jsonl'))
    text = capsys.readouterr().out
    assert 'Manual reviewer summary.' not in text
    assert 'Completed/attempted' in text
    if stopped is None:
        assert report['models']['nova']['notes'][0]['summary'] == 'Manual reviewer summary.'
    else:
        assert not client.calls and report['batch']['cost'] == '0'


@pytest.mark.parametrize('unsafe', ['input_report', 'audit_report', 'data_audit'])
def test_output_safeguards_before_client(generated, tmp_path, monkeypatch, unsafe):
    from ledgerguard.investigator_eval import main
    data = generated(7)
    audit, output = tmp_path / 'audit.jsonl', tmp_path / 'report.json'
    if unsafe == 'input_report': output = data / 'report.json'
    elif unsafe == 'audit_report': output = audit
    else: audit = data / 'audit.jsonl'
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('must not create client'))
    with pytest.raises(SystemExit):
        main(['--data', str(data), '--max-cost', '1', '--live', '--json', str(output), '--audit', str(audit)])


@pytest.mark.parametrize('reason,expected', [
    ('investigation invalid: notes schema at loc [["notes", 0, "summary"]]', 'investigation invalid: notes schema'),
    ('investigation invalid: notes schema at loc [["notes", 1, "uncertainty"]]', 'investigation invalid: notes schema'),
    ('investigation invalid: note count or index', 'investigation invalid: note count or index'),
    ('investigation invalid: unknown related invoice', 'investigation invalid: unknown related invoice'),
])
def test_specific_note_referrals_group_without_locations(reason, expected):
    from ledgerguard.investigator_eval import referral_category
    assert referral_category(reason) == expected


def test_referral_report_retains_schema_location_detail(generated, tmp_path):
    report = run(generated(7), tmp_path,
                 client=ScriptedClient(summary='x' * 601), models=('nova',), limit=1)
    referrals = report['models']['nova']['referrals']
    assert referrals['by_reason'] == {'investigation invalid: notes schema': 1}
    entry, = referrals['invoices']
    assert entry['invoice_number'] == report['selected_groups'][0]
    assert entry['reason'] == 'investigation invalid: notes schema'
    assert entry['detail'] == 'investigation invalid: notes schema at loc [["notes", 0, "summary"]]'
    assert 'x' * 601 not in entry['detail']
