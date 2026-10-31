"""Offline end-to-end evaluation, with truth preloaded only in the fake."""
import builtins
from decimal import Decimal
import json
from pathlib import Path

import boto3
import pytest

from fakes import FakeClient
import ledgerguard.pipeline_eval as module
from ledgerguard.gateway import DEFAULT_PRICES, MODEL_ID, NOVA_MODEL_ID
from ledgerguard.pipeline import PipelineModels


def run(case, tmp_path, *, client=None, **kwargs):
    root, pdfs, invoices, answers, _ = case
    client = client or FakeClient(pdfs, invoices, answers)
    report = module.compare(root, client=client, max_cost=kwargs.pop('max_cost', Decimal('5')),
                            audit_path=tmp_path / 'audit.jsonl', **kwargs)
    return report, client


def test_perfect_fakes_full_report(pipeline_case, tmp_path):
    report, client = run(pipeline_case, tmp_path)
    assert report['end_to_end']['overall']['caught'] == report['end_to_end']['overall']['expected'] == 14
    assert report['end_to_end']['overall']['recall'] == report['end_to_end']['overall']['precision'] == 1
    assert report['end_to_end']['clean_invoices'] == {'wrongly_flagged': 0, 'total': 20}
    assert report['baseline']['overall']['caught'] == 12
    assert report['extraction']['exact_matches'] == report['extraction']['selected'] == 38
    assert report['extraction']['accuracy'] == 1
    assert report['extraction']['invoice_number_mismatch_count'] == 0
    assert report['investigation'] == {'groups': 14, 'completed': 14, 'completion_rate': 1}
    assert set(report['notes']) == {f['invoice_numbers'][0] for f in report['findings']}
    assert report['referrals']['count'] == 0
    assert report['stopped'] is None and report['skipped_stages'] == []
    assert report['metadata']['model_ids'] == {'extract': NOVA_MODEL_ID, 'policy': NOVA_MODEL_ID,
                                              'investigate': MODEL_ID}
    assert report['batch']['max_calls'] == 38 * 20
    assert report['batch']['max_tool_calls'] == 38 * 10
    assert report['timing']['total_seconds'] >= 0
    assert report['timing']['per_invoice_seconds'] == report['timing']['total_seconds'] / 38
    assert Decimal(report['cost']['per_invoice']) == Decimal(report['cost']['total']) / 38
    output = module.render(report)
    assert '14/14' in output and 'notes: 14' in output
    for notes in report['notes'].values():
        assert all(note['summary'] not in output for note in notes)
    json.dumps(report)  # Public report is JSON serializable without a fallback encoder.


def test_truth_and_answers_read_only_after_pipeline(pipeline_case, tmp_path, monkeypatch):
    original_run = module.run_pipeline
    read_text, path_open, builtin_open = Path.read_text, Path.open, builtins.open
    state = {'running': False, 'returned': False}
    after = set()

    def check(path):
        if not isinstance(path, (str, Path)):
            return
        parts = Path(path).parts
        if 'invoices' in parts or 'answer_key' in parts:
            assert not state['running'], 'Truth read while pipeline running'
            assert state['returned'], 'Truth read before pipeline returned'
            after.add('answer_key' if 'answer_key' in parts else 'invoices')

    def read(path, *a, **kw):
        check(path)
        return read_text(path, *a, **kw)

    def opening(path, *a, **kw):
        check(path)
        return path_open(path, *a, **kw)

    def builtin(path, *a, **kw):
        check(path)
        return builtin_open(path, *a, **kw)

    def guarded_run(*a, **kw):
        state['running'] = True
        try:
            return original_run(*a, **kw)
        finally:
            state['running'] = False
            state['returned'] = True

    root, pdfs, invoices, answers, _ = pipeline_case
    client = FakeClient(pdfs, invoices, answers)
    monkeypatch.setattr(module, 'run_pipeline', guarded_run)
    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(Path, 'open', opening)
    monkeypatch.setattr(builtins, 'open', builtin)
    report, _ = run(pipeline_case, tmp_path, client=client)
    assert after == {'invoices', 'answer_key'}
    assert report['end_to_end']['overall']['caught'] == 14


def test_total_misread_changes_extraction_and_detection(pipeline_case, tmp_path):
    _, pdfs, invoices, answers, _ = pipeline_case
    client = FakeClient(pdfs, invoices, answers)
    client.records[pdfs['INV-2026-0028']]['total'] = '130.00'
    report, _ = run(pipeline_case, tmp_path, client=client)
    assert report['extraction']['exact_matches'] == 37
    assert report['extraction']['accuracy'] == 37 / 38
    assert report['end_to_end']['overall']['caught'] == 13
    assert report['end_to_end']['by_type']['arithmetic_error']['caught'] == 1
    assert report['baseline']['overall']['caught'] == 12


def test_number_mismatch_is_counted_without_crashing_scorer(pipeline_case, tmp_path):
    _, pdfs, invoices, answers, _ = pipeline_case
    client = FakeClient(pdfs, invoices, answers)
    client.records[pdfs['INV-2026-0028']]['invoice_number'] = 'MISREAD-0028'
    report, _ = run(pipeline_case, tmp_path, client=client)
    assert report['extraction']['exact_matches'] == 37
    assert report['extraction']['invoice_number_mismatches'] == ['INV-2026-0028']
    assert report['extraction']['invoice_number_mismatch_count'] == 1
    assert report['end_to_end']['overall']['caught'] == 14
    assert 'MISREAD-0028' in report['notes']


def test_stage_costs_and_largest_run(pipeline_case, tmp_path):
    report, _ = run(pipeline_case, tmp_path)
    total = sum((Decimal(stage['cost']) for stage in report['stages'].values()), Decimal(0))
    assert total == Decimal(report['batch']['cost'])
    assert report['cost']['stages_reconcile'] is True
    assert Decimal(report['cost']['unattributed']) == 0
    for name, calls, model in [('extraction', 38, NOVA_MODEL_ID), ('policy', 38, NOVA_MODEL_ID),
                               ('investigation', 14, MODEL_ID)]:
        stage = report['stages'][name]
        assert stage['calls'] == calls
        assert stage['tokens'] == {'input': calls * 100, 'output': calls * 100}
        assert Decimal(stage['cost']) == DEFAULT_PRICES[model].cost(calls * 100, calls * 100)
        assert stage['seconds'] >= 0
    budgets = report['budgets'].values()
    assert report['largest_run']['calls'] == max(b['calls'] for b in budgets) == 3
    assert Decimal(report['largest_run']['cost']) == max(Decimal(b['cost']) for b in budgets)
    assert report['largest_run']['within_limits'] is True
    assert sum(b['calls'] for b in budgets) == report['batch']['calls'] == 90


def test_limit_sorted_and_incomplete_answer_groups_excluded(pipeline_case, tmp_path):
    report, client = run(pipeline_case, tmp_path, limit=1)
    assert report['selected_documents'] == sorted(pipeline_case[1])[:1]
    assert len(client.calls) == 2  # No counterpart: no duplicate finding or investigation.
    assert report['end_to_end']['overall']['expected'] == 0
    smoke, _ = run(pipeline_case, tmp_path, limit=4)
    assert smoke['end_to_end']['overall']['caught'] == smoke['end_to_end']['overall']['expected'] == 2
    assert smoke['investigation']['completed'] == 2


def test_batch_stop_partial_report(pipeline_case, tmp_path):
    report, client = run(pipeline_case, tmp_path, max_cost=Decimal('0'))
    assert report['stopped'] == 'max_cost_exceeded'
    assert report['skipped_stages'] == ['extraction', 'rules', 'policy', 'investigation']
    assert not client.calls
    assert report['extraction']['successful'] == 0
    assert report['end_to_end']['overall']['expected'] == 14
    assert report['end_to_end']['overall']['caught'] == 0
    assert report['cost']['stages_reconcile']
    assert 'Skipped/unfinished stages: extraction' in module.render(report)


def test_model_overrides_meter_uses_request_model(pipeline_case, tmp_path):
    report, client = run(pipeline_case, tmp_path, limit=4,
                         models=PipelineModels(MODEL_ID, MODEL_ID, NOVA_MODEL_ID))
    assert report['metadata']['model_ids']['extract'] == MODEL_ID
    for stage, values in report['stages'].items():
        model = NOVA_MODEL_ID if stage == 'investigation' else MODEL_ID
        assert Decimal(values['cost']) == DEFAULT_PRICES[model].cost(
            values['tokens']['input'], values['tokens']['output'])


@pytest.mark.parametrize('arguments', [[], ['--live'], ['--max-cost', '0.50'],
                                       ['--live', '--max-cost', '-1'],
                                       ['--live', '--max-cost', 'NaN'],
                                       ['--live', '--max-cost', '0.50', '--limit', '0']])
def test_cli_safeguards_before_client(monkeypatch, arguments):
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('Must not create AWS client'))
    with pytest.raises(SystemExit) as caught:
        module.main(['--data', '/missing', *arguments])
    assert caught.value.code == 2


def test_cli_writes_json_and_hides_note_text(pipeline_case, tmp_path, monkeypatch, capsys):
    root, pdfs, invoices, answers, _ = pipeline_case
    client = FakeClient(pdfs, invoices, answers)
    def create(*a, **kw):
        assert kw['config'].retries == {'total_max_attempts': 1}
        return client
    monkeypatch.setattr(boto3, 'client', create)
    path = tmp_path / 'report.json'
    module.main(['--data', str(root), '--live', '--max-cost', '0.50', '--limit', '4',
                 '--json', str(path), '--audit', str(tmp_path / 'audit.jsonl')])
    report = json.loads(path.read_text())
    assert report['end_to_end']['overall']['caught'] == 2
    terminal = capsys.readouterr().out
    assert 'Evidence:' not in terminal and 'notes: 2' in terminal


def test_batch_stop_keeps_completed_extraction_and_measured_cost(pipeline_case, tmp_path):
    from ledgerguard.extractor import build_request
    from ledgerguard.gateway import estimate_input

    pdfs = pipeline_case[1]
    first = sorted(pdfs)[0]
    request = build_request(pdfs[first], NOVA_MODEL_ID)
    ceiling = DEFAULT_PRICES[NOVA_MODEL_ID].cost(estimate_input(request), 2000)
    report, client = run(pipeline_case, tmp_path, max_cost=ceiling)
    assert report['stopped'] == 'max_cost_exceeded'
    assert 0 < report['extraction']['successful'] < 38
    assert report['stages']['extraction']['calls'] == len(client.calls)
    assert report['stages']['policy']['calls'] == report['stages']['investigation']['calls'] == 0
    assert Decimal(report['cost']['total']) > 0
    assert report['cost']['stages_reconcile']
    assert report['skipped_stages'] == ['extraction', 'rules', 'policy', 'investigation']


@pytest.mark.parametrize('destination', ['inputs', 'same'])
def test_cli_rejects_unsafe_output_paths_before_client(pipeline_case, tmp_path, monkeypatch, destination):
    root = pipeline_case[0]
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('Must not create AWS client'))
    audit = tmp_path / 'audit.jsonl'
    output = root / 'report.json' if destination == 'inputs' else audit
    with pytest.raises(SystemExit) as caught:
        module.main(['--data', str(root), '--live', '--max-cost', '0.50',
                     '--json', str(output), '--audit', str(audit)])
    assert caught.value.code == 2


def test_meter_marks_unreconciled_usage_without_hiding_reserved_spend(pipeline_case, tmp_path):
    _, pdfs, invoices, answers, _ = pipeline_case
    class MissingUsage(FakeClient):
        def converse(self, **request):
            result = super().converse(**request)
            result.pop('usage')
            return result
    report, _ = run(pipeline_case, tmp_path, client=MissingUsage(pdfs, invoices, answers), limit=1)
    assert report['referrals']['by_stage']['extraction'] == 1
    assert report['stages']['extraction']['unpriced_calls'] == 1
    assert not report['cost']['stages_reconcile']
    assert Decimal(report['cost']['unattributed']) == Decimal(report['batch']['cost']) > 0
