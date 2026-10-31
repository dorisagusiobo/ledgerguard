"""Budgeted rules-plus-policy comparison on ground-truth invoices, not PDFs."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
from statistics import median

from ledgerguard.evaluate import load_expected, percentage, revision, score
from ledgerguard.extraction_eval import MODELS, MeasuredClient, model_order
from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, decimal_value
from ledgerguard.policy import AllClauses, check_policy
from ledgerguard.rules import load_inputs, run_rules


def compare(data: Path, *, client, max_cost: Decimal, audit_path: Path,
            split='dev', models=('nova', 'sonnet'), min_confidence=0.5, limit=None) -> dict:
    """Score all selected invoices even when policy calls stop partway.

    Rules run once and their results are shared across model comparisons. Limited
    cohorts score only answers whose full invoice group belongs to that cohort.
    """
    if split not in {'dev', 'test'} or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('Use split dev/test and a positive limit')
    if (isinstance(min_confidence, bool) or not isinstance(min_confidence, (int, float))
            or not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1):
        raise ValueError('min_confidence must be between zero and one')
    names = model_order(models)
    max_cost = decimal_value(max_cost)
    data = Path(data)
    inputs = load_inputs(data)
    splits = json.loads((data / 'splits.json').read_text())
    by_number = {i.invoice_number: i for i in inputs.invoices}
    if len(by_number) != len(inputs.invoices) or set(by_number) != set(splits):
        raise ValueError('Invoice numbers and splits.json must agree and be unique')
    selected = sorted(number for number in by_number if splits[number] == split)
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError('No invoices in selected split')
    scope = {number: split for number in selected}
    selected_set = set(selected)
    answers = [a for a in load_expected(data) if set(a['invoice_numbers']) <= selected_set]
    rules = run_rules([by_number[n] for n in selected], inputs.contracts, inputs.history, inputs.policy_clauses)
    baseline = score(rules.findings, answers, scope, split, rules.needs_review)
    retriever = AllClauses(inputs.policy_clauses)
    batch = Budget(max_calls=len(selected) * len(names) * Budget().max_calls, max_cost=max_cost)
    metadata_path = data / 'metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    report = {
        'metadata': {'seed': metadata.get('seed'), 'date': datetime.now(timezone.utc).isoformat(),
                     'git_revision': revision(), 'model_ids': {n: MODELS[n] for n in names},
                     'max_cost': str(max_cost), 'min_confidence': min_confidence},
        'split': split, 'limit': limit, 'selected_invoices': selected, 'stopped': None,
        'baseline': baseline, 'models': {},
        'scoring_scope': 'All selected invoices, including referrals and unattempted policy calls',
        'limitation': 'Dev has only two positive POL-4 cases; a perfect dev score is weak evidence. '
                      'Keep test held out until final evaluation.',
    }
    stopped = False
    for name in names:
        measured = MeasuredClient(client)
        flags, referrals, attempted = [], [], []
        cost = Decimal(0)
        for number in selected:
            if stopped:
                break
            invoice = by_number[number]
            budget = Budget()
            gateway = Gateway(client=measured, budget=budget, run_id=f'policy-{name}-{number}',
                              audit_path=audit_path, shared_budgets=(batch,))
            try:
                result = check_policy(invoice, retriever.retrieve(invoice), gateway=gateway,
                                      model_id=MODELS[name], min_confidence=min_confidence)
            except GatewayBlocked as error:
                if str(error) != 'max_cost_exceeded':
                    raise
                report['stopped'] = 'max_cost_exceeded'
                stopped = True
                break
            attempted.append(number)
            flags.extend(result.findings)
            referrals.extend(result.needs_review)
            cost += budget.cost
        combined = score([*rules.findings, *flags], answers, scope, split,
                         [*rules.needs_review, *referrals])
        report['models'][name] = {
            'model_id': MODELS[name], 'attempted': len(attempted),
            'not_attempted': [n for n in selected if n not in attempted],
            'rules_only': baseline, 'rules_plus_policy': combined,
            'policy_violation': combined['by_type']['policy_violation'],
            'policy_flags': [{'invoice_number': f.invoice_numbers[0], 'clause_id': f.citation.clause_id,
                              'confidence': f.confidence} for f in flags],
            'false_flags': combined['false_flags'],
            'referrals': {'count': len(referrals),
                          'by_reason': dict(Counter(r.reason.split(':', 1)[0] for r in referrals)),
                          'invoices': [{'invoice_number': r.invoice_number,
                                        'reason': r.reason.split(':', 1)[0]} for r in referrals]},
            'cost': {'total': str(cost), 'per_invoice': str(cost / len(attempted)) if attempted else None},
            'tokens': {'input': measured.input_tokens, 'output': measured.output_tokens},
            'timing': {'calls': len(measured.durations),
                       'median_seconds': median(measured.durations) if measured.durations else None,
                       'slowest_seconds': max(measured.durations) if measured.durations else None},
        }
    report['batch'] = {'calls': batch.calls, 'cost': str(batch.cost), 'max_cost': str(max_cost)}
    return report


def render(report):
    rows = [f"Split: {report['split']} | Stopped: {report['stopped'] or 'no'}",
            'Model | Attempted | Recall | Precision | Clean flagged | Policy caught/expected | Cost | Cost/invoice']

    def row(name, scored, attempted='-', cost='-', per_invoice='-'):
        clean, policy = scored['clean_invoices'], scored['by_type']['policy_violation']
        return (f"{name} | {attempted} | {percentage(scored['overall']['recall'])} | "
                f"{percentage(scored['overall']['precision'])} | {clean['wrongly_flagged']}/{clean['total']} | "
                f"{policy['caught']}/{policy['expected']} | {cost} | {per_invoice}")

    rows.append(row('rules only', report['baseline']))
    for name, model in report['models'].items():
        rows.append(row(name + ' + rules', model['rules_plus_policy'], model['attempted'],
                        model['cost']['total'], model['cost']['per_invoice'] or 'n/a'))
        rows.append('  Policy flags: ' + ('; '.join(
            f"{f['invoice_number']} [{f['clause_id']}] confidence={f['confidence']}" for f in model['policy_flags']) or 'none'))
        rows.append('  False flags: ' + ('; '.join(', '.join(f['invoice_numbers']) for f in model['false_flags']) or 'none'))
        rows.append(f"  Referrals: {model['referrals']['count']} {model['referrals']['by_reason']}")
        rows.append(f"  Tokens: {model['tokens']}; timing: {model['timing']}")
    rows += [f"Batch cost: ${report['batch']['cost']} / ${report['batch']['max_cost']}", report['limitation']]
    return '\n'.join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--split', choices=['dev', 'test'], default='dev')
    parser.add_argument('--models', default='nova,sonnet')
    parser.add_argument('--max-cost', required=True)
    parser.add_argument('--min-confidence', type=float, default=0.5)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--json', type=Path, dest='json_path')
    parser.add_argument('--audit', type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('--live is required; no AWS client was created')
    try:
        names = model_order(args.models.split(','))
        max_cost = decimal_value(args.max_cost)
        if not math.isfinite(args.min_confidence) or not 0 <= args.min_confidence <= 1:
            raise ValueError('--min-confidence must be between zero and one')
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        audit = args.audit or Path('runs') / f'policy-{stamp}.jsonl'
        if 'data' in audit.resolve().parts:
            raise ValueError('Audit log must be outside data directories')
        if args.json_path and (args.json_path.resolve().is_relative_to(args.data.resolve())
                               or args.json_path.resolve() == audit.resolve()):
            raise ValueError('JSON report must be outside inputs and separate from audit log')
    except (ValueError, ArithmeticError) as error:
        parser.error(str(error))
    import boto3
    from botocore.config import Config
    client = boto3.client('bedrock-runtime', region_name='us-east-1',
                          config=Config(retries={'total_max_attempts': 1}))
    report = compare(args.data, client=client, max_cost=max_cost, audit_path=audit, split=args.split,
                     models=names, limit=args.limit, min_confidence=args.min_confidence)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(render(report))


if __name__ == '__main__':
    main()
