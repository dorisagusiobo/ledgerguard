"""Scoring separates detection, money accuracy, and clause accuracy."""
from decimal import Decimal
import json
import shutil
from pathlib import Path
import subprocess
import sys

import pytest

from ledgerguard.evaluate import evaluate, render, score
from ledgerguard.models import Citation, Finding, SourceRef
from ledgerguard.rules import ReviewReferral


SPLITS = {'INV-2026-0001': 'dev', 'INV-2026-0002': 'dev',
          'INV-2026-0003': 'dev', 'INV-2026-0004': 'test'}


def answer(numbers=None, amount='10.00', clause=None, kind='duplicate', split='dev'):
    return {'type': kind, 'invoice_numbers': numbers or ['INV-2026-0001', 'INV-2026-0002'],
            'amount_at_issue': amount, 'clause_id': clause, 'split': split}


def prediction(expected=None, **changes):
    data = {**(expected or answer()), **changes}
    clause = data['clause_id']
    return Finding(type=data['type'], invoice_numbers=data['invoice_numbers'],
                   amount_at_issue=data['amount_at_issue'], calculation='Synthetic calculation',
                   evidence=[SourceRef(page=1, text='Synthetic evidence')],
                   citation=Citation(document='policy.md', version='v1', clause_id=clause,
                                     quote='Synthetic clause text') if clause else None,
                   no_clause_reason=None if clause else 'no applicable clause identified', confidence=1)


def test_perfect_predictions():
    expected = [answer(), answer(['INV-2026-0003'], clause='POL-1', kind='policy_violation')]
    report = score([prediction(a) for a in expected], expected, SPLITS)
    overall = report['overall']
    assert overall['expected'] == overall['caught'] == 2
    assert overall['missed'] == overall['false_flags'] == 0
    for metric in ['recall', 'precision', 'amount_accuracy', 'citation_accuracy']:
        assert overall[metric] == 1
    assert report['by_type']['duplicate']['recall'] == 1


def test_missing_and_extra_findings():
    expected = [answer(), answer(['INV-2026-0002'], kind='arithmetic_error')]
    extra = prediction(answer(['INV-2026-0003'], kind='unusual_amount'))
    report = score([prediction(), extra], expected, SPLITS)
    assert report['overall']['caught'] == 1
    assert report['overall']['missed'] == 1
    assert report['overall']['false_flags'] == 1
    assert report['overall']['recall'] == report['overall']['precision'] == 0.5
    assert report['clean_invoices'] == {'wrongly_flagged': 1, 'total': 1}


def test_wrong_amount_does_not_hide_detection():
    report = score([prediction(amount_at_issue='10.01')], [answer()], SPLITS)
    assert report['overall']['caught'] == 1
    assert report['overall']['recall'] == report['overall']['precision'] == 1
    assert report['overall']['amount_accuracy'] == 0
    assert report['overall']['citation_accuracy'] == 1


@pytest.mark.parametrize('expected_clause,actual_clause', [(None, 'POL-1'), ('POL-1', None), ('POL-1', 'POL-3')])
def test_wrong_clause_only_lowers_citation_accuracy(expected_clause, actual_clause):
    expected = answer(clause=expected_clause)
    report = score([prediction(expected, clause_id=actual_clause)], [expected], SPLITS)
    assert report['overall']['caught'] == 1
    assert report['overall']['citation_accuracy'] == 0
    assert report['overall']['amount_accuracy'] == 1


def test_one_to_one_duplicate_prediction():
    report = score([prediction(), prediction()], [answer()], SPLITS)
    assert report['overall']['caught'] == 1
    assert report['overall']['false_flags'] == 1
    assert report['overall']['precision'] == 0.5


def test_invoice_order_is_irrelevant():
    expected = answer()
    actual = prediction(invoice_numbers=list(reversed(expected['invoice_numbers'])))
    assert score([actual], [expected], SPLITS)['overall']['caught'] == 1


def test_no_findings_and_empty_ground_truth():
    report = score([], [answer()], SPLITS)
    assert report['overall']['recall'] == 0
    assert report['overall']['precision'] is None
    assert report['overall']['amount_accuracy'] is None
    assert report['overall']['citation_accuracy'] is None
    assert 'n/a' in render(report)
    empty = score([], [], SPLITS)
    assert empty['overall']['recall'] is None
    assert empty['clean_invoices'] == {'wrongly_flagged': 0, 'total': 3}


def test_split_filtering_and_referrals():
    held_out = answer(['INV-2026-0004'], split='test')
    referrals = [ReviewReferral('INV-2026-0003', 'Review currency'),
                 ReviewReferral('INV-2026-0004', 'Review currency')]
    report = score([prediction(), prediction(held_out)], [answer(), held_out], SPLITS,
                   needs_review=referrals)
    assert report['split'] == 'dev'
    assert report['overall']['expected'] == report['overall']['caught'] == 1
    assert report['referred_to_review'] == 1
    held_out_report = score([prediction(), prediction(held_out)], [answer(), held_out], SPLITS, 'test', referrals)
    assert held_out_report['overall']['caught'] == 1
    assert held_out_report['clean_invoices']['total'] == 0


def test_cross_split_group_rejected():
    with pytest.raises(ValueError, match='Cross-split'):
        score([prediction(invoice_numbers=['INV-2026-0001', 'INV-2026-0004'])], [], SPLITS)


def test_clean_flag_count_counts_invoices_not_findings():
    expected = answer(['INV-2026-0001'])
    extra = prediction(answer(['INV-2026-0003'], kind='unusual_amount'))
    report = score([extra, extra], [expected], SPLITS)
    assert report['overall']['false_flags'] == 2
    assert report['clean_invoices'] == {'wrongly_flagged': 1, 'total': 2}


@pytest.mark.parametrize('seed', [7, 8, 9])
def test_generated_dev_baseline(generated, seed):
    report = evaluate(generated(seed))
    assert report['split'] == 'dev'
    assert report['overall'] == {
        'expected': 14, 'caught': 12, 'missed': 2, 'false_flags': 0,
        'recall': 12 / 14, 'precision': 1, 'amount_accuracy': 1, 'citation_accuracy': 1}
    assert report['clean_invoices']['wrongly_flagged'] == 0
    assert report['referred_to_review'] == 0
    assert report['by_type']['policy_violation']['missed'] == 2
    assert sum(row['missed'] for kind, row in report['by_type'].items() if kind != 'policy_violation') == 0
    assert report['metadata']['seed'] == seed
    assert report['metadata']['date']
    assert report['metadata']['git_revision']


def test_cli_defaults_to_dev_and_writes_json(tmp_path, generated):
    data = generated(7)
    output = tmp_path / 'report.json'
    result = subprocess.run([sys.executable, '-m', 'ledgerguard.evaluate', '--data', str(data),
                             '--json', str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Split: dev' in result.stdout
    assert '85.7%' in result.stdout
    assert 'Clean invoices wrongly flagged: 0 of' in result.stdout
    report = json.loads(output.read_text())
    assert report['overall']['caught'] == 12
    assert report['metadata']['seed'] == 7
    assert len(report['missed_answers']) == 2
    assert all(a['clause_id'] == 'POL-4' for a in report['missed_answers'])
    assert report['false_flags'] == []
    assert 'Missed: policy_violation' in result.stdout
    assert '[POL-4]' in result.stdout
    assert 'False flags: none' in result.stdout


def test_legacy_dataset_does_not_invent_seed(tmp_path, generated):
    shutil.copytree(generated(7), tmp_path, dirs_exist_ok=True)
    (tmp_path / 'metadata.json').unlink()
    assert evaluate(tmp_path)['metadata']['seed'] is None


def test_missed_and_false_flag_details_match_counts_and_split():
    missing = answer(['INV-2026-0002'], kind='policy_violation', clause='POL-4')
    held_out = answer(['INV-2026-0004'], split='test')
    extra = prediction(answer(['INV-2026-0003'], kind='unusual_amount', amount='15.00'))
    report = score([prediction(), extra, extra], [answer(), missing, held_out], SPLITS)
    assert report['missed_answers'] == [
        {'type': 'policy_violation', 'invoice_numbers': ['INV-2026-0002'],
         'amount_at_issue': '10.00', 'clause_id': 'POL-4'}]
    assert report['false_flags'] == [
        {'type': 'unusual_amount', 'invoice_numbers': ['INV-2026-0003'],
         'amount_at_issue': '15.00', 'clause_id': None}] * 2
    assert len(report['missed_answers']) == report['overall']['missed']
    assert len(report['false_flags']) == report['overall']['false_flags']
    text = render(report)
    assert 'Missed: policy_violation INV-2026-0002 [POL-4]' in text
    assert 'False flags: unusual_amount INV-2026-0003 [no clause]' in text


def test_correct_detection_with_wrong_amount_is_not_listed_as_missed():
    report = score([prediction(amount_at_issue='11.00')], [answer()], SPLITS)
    assert report['missed_answers'] == report['false_flags'] == []
    assert 'Missed: none' in render(report)
    assert 'False flags: none' in render(report)
