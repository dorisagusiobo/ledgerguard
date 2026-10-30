"""Offline evaluation boundary: only this runtime module reads answer_key/."""
import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import subprocess
from typing import get_args
from zoneinfo import ZoneInfo

from ledgerguard.models import Finding, FindingType
from ledgerguard.rules import ReviewReferral, load_inputs, run_rules


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def metrics(expected, caught, false_flags, amount_correct, citation_correct):
    return {'expected': expected, 'caught': caught, 'missed': expected - caught,
            'false_flags': false_flags, 'recall': ratio(caught, expected),
            'precision': ratio(caught, caught + false_flags),
            'amount_accuracy': ratio(amount_correct, caught),
            'citation_accuracy': ratio(citation_correct, caught)}


def detection_key(kind, numbers):
    return kind, tuple(sorted(set(numbers)))


def score(findings: list[Finding], expected: list[dict], splits: dict[str, str],
          split: str = 'dev', needs_review: list[ReviewReferral] | None = None) -> dict:
    """One-to-one detection; amount/clause accuracy never changes recall.

    Findings entirely in another split are ignored. Cross-split groups are rejected,
    since evaluating them would mix held-out and development inputs. Run rules on
    the selected split first, as evaluate() does.
    """
    if split not in {'dev', 'test'}:
        raise ValueError('Split must be dev or test')
    selected = {number for number, label in splits.items() if label == split}

    def belongs(numbers):
        numbers = set(numbers)
        if not numbers or not numbers <= splits.keys():
            raise ValueError('Finding/answer references missing invoice numbers')
        labels = {splits[number] for number in numbers}
        if len(labels) != 1:
            raise ValueError('Cross-split finding/answer must not be evaluated')
        return numbers <= selected

    predictions = [f for f in findings if belongs(f.invoice_numbers)]
    answers = [a for a in expected if belongs(a['invoice_numbers'])]
    for answer in answers:
        if answer['split'] != split:
            raise ValueError('Answer split disagrees with splits.json')
    pending = defaultdict(list)
    for answer in answers:
        pending[detection_key(answer['type'], answer['invoice_numbers'])].append(answer)
    counts = {kind: {'expected': 0, 'caught': 0, 'false_flags': 0,
                     'amount_correct': 0, 'citation_correct': 0} for kind in get_args(FindingType)}
    for answer in answers:
        counts[answer['type']]['expected'] += 1
    # Prefer accurate copies first when duplicate predictions are present; input
    # invoice order and prediction order must not change the score.
    def accuracy_priority(f):
        clause = f.citation.clause_id if f.citation else None
        candidates = pending[detection_key(f.type, f.invoice_numbers)]
        return max((int(f.amount_at_issue == Decimal(a['amount_at_issue']))
                    + int(clause == a['clause_id']) for a in candidates), default=-1)

    ordered = sorted(predictions, key=lambda f: (-accuracy_priority(f), f.type,
                     sorted(f.invoice_numbers), f.amount_at_issue,
                     f.citation.clause_id if f.citation else ''))
    false_flags = []
    for finding in ordered:
        count = counts[finding.type]
        candidates = pending[detection_key(finding.type, finding.invoice_numbers)]
        if not candidates:
            count['false_flags'] += 1
            false_flags.append({'type': finding.type,
                                'invoice_numbers': sorted(set(finding.invoice_numbers)),
                                'amount_at_issue': str(finding.amount_at_issue),
                                'clause_id': finding.citation.clause_id if finding.citation else None})
            continue
        clause = finding.citation.clause_id if finding.citation else None
        answer = max(candidates, key=lambda a: (
            int(finding.amount_at_issue == Decimal(a['amount_at_issue'])) + int(clause == a['clause_id']),
            a['amount_at_issue'], a['clause_id'] or ''))
        candidates.remove(answer)
        count['caught'] += 1
        count['amount_correct'] += finding.amount_at_issue == Decimal(answer['amount_at_issue'])
        count['citation_correct'] += clause == answer['clause_id']
    per_type = {kind: metrics(**count) for kind, count in counts.items()}
    overall = metrics(**{name: sum(count[name] for count in counts.values())
                         for name in next(iter(counts.values()))})
    clean = selected - {number for a in answers for number in a['invoice_numbers']}
    wrongly_flagged = clean & {number for f in predictions for number in f.invoice_numbers}
    referrals = {r.invoice_number for r in needs_review or [] if r.invoice_number in selected}
    def detail_key(item):
        return item['type'], item['invoice_numbers'], item['clause_id'] or '', item['amount_at_issue']

    missed = [{'type': a['type'], 'invoice_numbers': sorted(set(a['invoice_numbers'])),
               'amount_at_issue': a['amount_at_issue'], 'clause_id': a['clause_id']}
              for remaining in pending.values() for a in remaining]
    return {'split': split, 'overall': overall, 'by_type': per_type,
            'missed_answers': sorted(missed, key=detail_key),
            'false_flags': sorted(false_flags, key=detail_key),
            'clean_invoices': {'wrongly_flagged': len(wrongly_flagged), 'total': len(clean)},
            'referred_to_review': len(referrals)}


def revision() -> str | None:
    try:
        result = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                                cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def evaluate(data: Path, split: str = 'dev') -> dict:
    data = Path(data)
    splits = json.loads((data / 'splits.json').read_text())
    inputs = load_inputs(data)
    if {i.invoice_number for i in inputs.invoices} != set(splits):
        raise ValueError('Invoice files and splits.json must describe the same invoices')
    invoices = [i for i in inputs.invoices if splits[i.invoice_number] == split]
    result = run_rules(invoices, inputs.contracts, inputs.history, inputs.policy_clauses)
    # Answers enter only the scorer, never rules or their loader.
    answers = json.loads((data / 'answer_key/expected_findings.json').read_text())
    report = score(result.findings, answers, splits, split, result.needs_review)
    metadata_path = data / 'metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    report['metadata'] = {'seed': metadata.get('seed'),
                          'date': datetime.now(ZoneInfo('America/New_York')).isoformat(),
                          'git_revision': revision()}
    return report


def percentage(value):
    return 'n/a' if value is None else f'{value:.1%}'


def render(report: dict) -> str:
    lines = [f"Split: {report['split']}",
             'Type | Expected | Caught | Missed | False flags | Recall | Precision | Amount accuracy | Citation accuracy']
    for kind, row in [('overall', report['overall']), *report['by_type'].items()]:
        values = [str(row[key]) for key in ['expected', 'caught', 'missed', 'false_flags']]
        values += [percentage(row[key]) for key in ['recall', 'precision', 'amount_accuracy', 'citation_accuracy']]
        lines.append(' | '.join([kind, *values]))
    def describe(items):
        return '; '.join(f"{item['type']} {', '.join(item['invoice_numbers'])} [{item['clause_id'] or 'no clause'}]"
                         for item in items) or 'none'

    lines += [f"Missed: {describe(report['missed_answers'])}",
              f"False flags: {describe(report['false_flags'])}"]
    clean = report['clean_invoices']
    lines += [f"Clean invoices wrongly flagged: {clean['wrongly_flagged']} of {clean['total']}",
              f"Referred to review: {report['referred_to_review']}"]
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('data/synthetic'))
    parser.add_argument('--split', choices=['dev', 'test'], default='dev')
    parser.add_argument('--json', type=Path, dest='json_path')
    args = parser.parse_args()
    try:
        report = evaluate(args.data, args.split)
        if args.json_path:
            destination = args.json_path.resolve()
            # A report must never overwrite source data or evaluation answers.
            if destination.is_relative_to(args.data.resolve()):
                raise ValueError('Save reports outside the input dataset')
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
        print(render(report))
    except (OSError, ValueError, KeyError) as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
