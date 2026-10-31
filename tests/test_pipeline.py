"""Offline PDF pipeline: the fake alone preloads invoice/answer JSON."""
from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
import builtins
import json
from pathlib import Path

import pytest

import ledgerguard.pipeline as pipeline
from ledgerguard.evaluate import score
from ledgerguard.gateway import Budget, MODEL_ID, NOVA_MODEL_ID
from ledgerguard.rules import load_reference


from fakes import FakeClient


def run(case, tmp_path, *, batch=None, **fake_options):
    root, pdfs, invoices, answers, _ = case
    client = FakeClient(pdfs, invoices, answers, **fake_options)
    result = pipeline.run_pipeline(pdfs, load_reference(root), client=client,
                                   audit_path=tmp_path / 'audit.jsonl',
                                   models=pipeline.PipelineModels(),
                                   batch=batch or Budget(label='batch', max_calls=1000,
                                                         max_cost=Decimal('5'), max_tool_calls=1000))
    return result, client


def test_perfect_pdf_pipeline_and_shared_invoice_accounting(case, tmp_path, monkeypatch):
    original, rules_calls = pipeline.run_rules, []
    before = []
    original_policy = pipeline.check_policy

    def rules_once(*args, **kwargs):
        rules_calls.append(1)
        result = original(*args, **kwargs)
        before.extend(deepcopy(result.findings))
        return result

    def policy_capture(*args, **kwargs):
        result = original_policy(*args, **kwargs)
        before.extend(deepcopy(result.findings))
        return result

    monkeypatch.setattr(pipeline, 'run_rules', rules_once)
    monkeypatch.setattr(pipeline, 'check_policy', policy_capture)
    result, client = run(case, tmp_path, tool=True)
    report = score(result.findings, case[3], case[4])['overall']
    assert report['caught'] == report['expected'] == 14
    assert report['precision'] == report['recall'] == 1
    assert rules_calls == [1]
    assert result.findings == before
    groups = {f.invoice_numbers[0] for f in result.findings}
    assert set(result.notes) == groups and all(result.notes.values())
    assert not result.referrals and result.stopped is None and result.skipped_stages == []
    assert len(result.invoices) == 38
    records = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    for document_id, invoice in result.invoices.items():
        logs = [r for r in records if r['run_id'] == f'{result.pipeline_run_id}:{document_id}' and 'model' in r]
        expected = 4 if invoice.invoice_number in groups else 2
        assert len(logs) == result.budgets[document_id]['calls'] == expected
        assert [r['model'] for r in logs[:2]] == [NOVA_MODEL_ID, NOVA_MODEL_ID]
        assert all(r['model'] == MODEL_ID for r in logs[2:])
        assert result.budgets[document_id]['cost'] == sum((Decimal(r['cost']) for r in logs), Decimal(0))
    assert {r['run_id'] for r in records} == {f'{result.pipeline_run_id}:{n}' for n in case[1]}
    assert all(r['row_count'] >= 1 for r in records if r.get('phase') == 'completion')


def test_no_invoice_json_or_answers_read_during_pipeline(case, tmp_path, monkeypatch):
    root, pdfs, invoices, answers, _ = case
    client = FakeClient(pdfs, invoices, answers)
    original_read, original_open, original_path_open = Path.read_text, builtins.open, Path.open

    def check(path):
        if isinstance(path, (str, Path)):
            parts = Path(path).parts
            assert 'answer_key' not in parts
            assert not ('invoices' in parts and str(path).endswith('.json'))

    def read(path, *a, **kw):
        check(path)
        return original_read(path, *a, **kw)

    def opening(path, *a, **kw):
        check(path)
        return original_open(path, *a, **kw)

    def path_open(path, *a, **kw):
        check(path)
        return original_path_open(path, *a, **kw)

    monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(Path, 'open', path_open)
    monkeypatch.setattr(builtins, 'open', opening)
    result = pipeline.run_pipeline(pdfs, load_reference(root), client=client,
                                   audit_path=tmp_path / 'audit.jsonl', models=pipeline.PipelineModels(),
                                   batch=Budget(max_calls=1000, max_cost='5', max_tool_calls=1000))
    assert len(result.findings) == 14 and not result.referrals


def test_failed_extraction_continues(case, tmp_path):
    result, _ = run(case, tmp_path, fail='INV-2026-0027')
    assert 'INV-2026-0027' not in result.invoices
    assert all('INV-2026-0027' not in f.invoice_numbers for f in result.findings)
    assert result.referrals == [{'document_id': 'INV-2026-0027', 'stage': 'extraction',
                                 'reason': 'extraction incomplete: end_turn'}]
    assert len(result.findings) == 13 and result.stopped is None
    assert set(result.notes) == {f.invoice_numbers[0] for f in result.findings}


def test_repeated_number_is_extraction_referral(case, tmp_path):
    result, _ = run(case, tmp_path, repeat=('INV-2026-0027', 'INV-2026-0001'))
    assert len(result.invoices) == 37
    assert result.referrals == [{'document_id': 'INV-2026-0027', 'stage': 'extraction',
                                 'reason': 'duplicate invoice number in batch'}]
    assert result.stopped is None


def test_invoice_budget_in_investigation_continues(case, tmp_path):
    result, client = run(case, tmp_path, expensive='INV-2026-0028')
    assert result.referrals == [{'document_id': 'INV-2026-0028', 'stage': 'budget',
                                 'reason': 'max_cost_exceeded'}]
    assert 'INV-2026-0028' not in result.notes
    assert 'INV-2026-0036' in result.notes and result.stopped is None
    assert len(result.findings) == 14
    logs = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    blocks = [r for r in logs if r['decision'] == 'block']
    assert len(blocks) == 1 and blocks[0]['scope'] == 'invoice'
    assert result.budgets['INV-2026-0028']['calls'] == 3


@pytest.mark.parametrize('stage', ['extraction', 'policy', 'investigation'])
def test_batch_block_stops_later_calls_and_returns_partial(case, tmp_path, monkeypatch, stage):
    # Exhaust the shared budget immediately before the second call in this stage.
    batch = Budget(label='batch', max_calls=1000, max_cost='5', max_tool_calls=1000)
    original = pipeline.Gateway.converse
    seen = Counter()

    def converse(gateway, model, request):
        names = {t['toolSpec']['name'] for t in request['toolConfig']['tools']}
        current = ('extraction' if 'record_invoice' in names else
                   'policy' if 'record_policy_findings' in names else 'investigation')
        seen[current] += 1
        if current == stage and seen[current] == 2:
            batch.max_cost = batch.cost
        return original(gateway, model, request)

    monkeypatch.setattr(pipeline.Gateway, 'converse', converse)
    result, client = run(case, tmp_path, batch=batch)
    assert result.stopped == 'max_cost_exceeded'
    assert result.skipped_stages == list(pipeline.STAGES[pipeline.STAGES.index(stage):])
    assert client.calls[-1][0] == stage
    assert seen[stage] == 2
    assert len(client.calls) == sum(seen.values()) - 1
    logs = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert logs[-1]['scope'] == 'batch' and logs[-1]['decision'] == 'block'
    if stage == 'extraction':
        assert len(result.invoices) == 1 and not result.findings
    else:
        assert len(result.invoices) == 38 and result.findings
    if stage == 'investigation':
        assert len(result.notes) == 1


def test_model_defaults_frozen():
    models = pipeline.PipelineModels()
    assert (models.extract, models.policy, models.investigate) == (NOVA_MODEL_ID, NOVA_MODEL_ID, MODEL_ID)
    with pytest.raises(FrozenInstanceError):
        models.extract = MODEL_ID


def test_document_ids_are_independent_of_invoice_numbers(case, tmp_path):
    root, pdfs, invoices, answers, _ = case
    client = FakeClient(pdfs, invoices, answers)
    renamed = {f'document-{index}': pdf for index, pdf in enumerate(pdfs.values())}
    result = pipeline.run_pipeline(renamed, load_reference(root), client=client,
                                   audit_path=tmp_path / 'audit.jsonl', models=pipeline.PipelineModels(),
                                   batch=Budget(max_calls=1000, max_cost='5', max_tool_calls=1000))
    assert set(result.invoices) == set(result.budgets) == set(renamed)
    assert set(result.notes) == {f.invoice_numbers[0] for f in result.findings}
    logs = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert {r['run_id'] for r in logs} == {f'{result.pipeline_run_id}:{n}' for n in renamed}
    assert len(result.findings) == 14


def test_non_usd_is_rules_referral(case, tmp_path):
    root, pdfs, invoices, answers, _ = case
    client = FakeClient(pdfs, invoices, answers)
    client.records[pdfs['INV-2026-0027']]['currency'] = 'EUR'
    result = pipeline.run_pipeline(pdfs, load_reference(root), client=client,
                                   audit_path=tmp_path / 'audit.jsonl', models=pipeline.PipelineModels(),
                                   batch=Budget(max_calls=1000, max_cost='5', max_tool_calls=1000))
    assert result.invoices['INV-2026-0027'].currency == 'EUR'
    assert {'invoice_number': 'INV-2026-0027', 'stage': 'rules',
            'reason': 'Unsupported currency EUR; requires human review'} in result.referrals
    assert not any('INV-2026-0027' in f.invoice_numbers for f in result.findings)
    assert result.stopped is None


@pytest.mark.parametrize('stage', ['extraction', 'policy'])
def test_invoice_block_in_earlier_stages_skips_later_work(case, tmp_path, monkeypatch, stage):
    original = pipeline.Gateway.converse
    target = 'INV-2026-0027'

    def converse(gateway, model, request):
        name = request['toolConfig']['tools'][0]['toolSpec']['name']
        current = 'extraction' if name == 'record_invoice' else 'policy'
        if gateway.run_id.endswith(f':{target}') and current == stage:
            gateway.budget.max_cost = gateway.budget.cost
        return original(gateway, model, request)

    monkeypatch.setattr(pipeline.Gateway, 'converse', converse)
    result, client = run(case, tmp_path)
    assert {'document_id': target, 'stage': 'budget', 'reason': 'max_cost_exceeded'} in result.referrals
    assert result.stopped is None and target not in result.notes
    assert 'INV-2026-0035' in result.notes
    assert result.budgets[target]['calls'] == (0 if stage == 'extraction' else 1)
    if stage == 'policy':
        assert any(target in f.invoice_numbers for f in result.findings)
    else:
        assert target not in result.invoices


def test_undersized_batch_refused_before_calls(case, tmp_path):
    batch = Budget(label='batch', max_calls=len(case[1]) - 1, max_cost='5')
    with pytest.raises(ValueError, match='cover extraction'):
        run(case, tmp_path, batch=batch)
    assert batch.calls == 0 and batch.cost == 0
    assert not (tmp_path / 'audit.jsonl').exists()


def test_explicit_pipeline_run_id_is_returned_and_used(case, tmp_path):
    root, pdfs, invoices, answers, _ = case
    result = pipeline.run_pipeline(pdfs, load_reference(root), client=FakeClient(pdfs, invoices, answers),
                                   audit_path=tmp_path / 'audit.jsonl', models=pipeline.PipelineModels(),
                                   batch=Budget(label='batch', max_calls=760, max_tool_calls=380, max_cost='5'),
                                   pipeline_run_id='synthetic-explicit-run')
    assert result.pipeline_run_id == 'synthetic-explicit-run'
    logs = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert {r['run_id'] for r in logs} == {f'{result.pipeline_run_id}:{number}' for number in pdfs}
