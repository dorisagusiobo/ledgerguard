"""Budgeted investigator reliability evaluation on rules-only findings.

Manual note grading checklist (there is no automated answer key for good notes):
1. Is it accurate, consistent with the finding and the tool results?
2. Does it name the evidence it used?
3. Does it say how certain it is?
4. Does it avoid calling anything fraud?
5. Is it useful? Would a reviewer decide faster with it?

Notes are intentionally included in JSON for manual grading on synthetic data.
Terminal output and gateway audit records never include note summaries.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from ledgerguard.evaluate import percentage, ratio, revision
from ledgerguard.extraction_eval import MODELS, MeasuredClient, model_order
from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, decimal_value
from ledgerguard.investigator import Investigation, investigate
from ledgerguard.rules import ReviewReferral, load_inputs, run_rules
from ledgerguard.tools import ReadOnlyTools

EXPECTED_TOOLS = {'duplicate': 'duplicate_search', 'split_purchase': 'related_invoices',
                  'unusual_amount': 'vendor_history'}


class CountedTools:
    """Count real dispatches, not rejected attempts; retain no arguments/results."""
    def __init__(self, tools):
        self.tools, self.calls = tools, Counter()

    def vendor_history(self, **arguments):
        self.calls['vendor_history'] += 1
        return self.tools.vendor_history(**arguments)

    def related_invoices(self, **arguments):
        self.calls['related_invoices'] += 1
        return self.tools.related_invoices(**arguments)

    def duplicate_search(self, **arguments):
        self.calls['duplicate_search'] += 1
        return self.tools.duplicate_search(**arguments)


def distribution(values):
    return {'median': median(values) if values else None, 'max': max(values) if values else None}


def referral_category(reason):
    return {
        'investigation stopped: turn limit': 'turn cap',
        'investigation stopped: tool limit': 'tool cap',
        'investigation invalid: submitted notes': 'invalid notes',
        'investigation failed: model call': 'failed model call',
        'investigation failed: tool call': 'failed tool call',
        'investigation invalid: model response': 'invalid model response',
        'investigation stopped: graph recursion limit': 'graph recursion cap',
        'investigation stopped: batch cost limit': 'batch cost limit',
    }.get(reason, 'other')


def compare(data: Path, *, client, max_cost: Decimal, audit_path: Path,
            split='dev', models=('nova', 'sonnet'), max_turns=6, limit=None) -> dict:
    """No answers are loaded. Limit applies to sorted rules-finding groups.

    Expected-tool rates use attempted groups containing the finding type as their
    denominator. Blocked tool attempts count toward caps; executed tools are also
    reported separately. Partly paid investigations count as attempted on a stop.
    """
    if split not in {'dev', 'test'} or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('Use split dev/test and a positive limit')
    if type(max_turns) is not int or max_turns < 1:
        raise ValueError('max_turns must be a positive integer')
    names, max_cost, data = model_order(models), decimal_value(max_cost), Path(data)
    inputs = load_inputs(data)
    splits = json.loads((data / 'splits.json').read_text())
    by_number = {i.invoice_number: i for i in inputs.invoices}
    if len(by_number) != len(inputs.invoices) or set(by_number) != set(splits):
        raise ValueError('Invoice numbers and splits.json must agree and be unique')
    invoices = [by_number[n] for n in sorted(by_number) if splits[n] == split]
    rules = run_rules(invoices, inputs.contracts, inputs.history, inputs.policy_clauses)
    groups = defaultdict(list)
    for finding in rules.findings:
        groups[finding.invoice_numbers[0]].append(finding)
    selected = sorted(groups)
    if limit is not None:
        selected = selected[:limit]
    tools = ReadOnlyTools(invoices, inputs.history)
    batch = Budget(max_calls=len(selected) * len(names) * max_turns,
                   max_tool_calls=len(selected) * len(names) * 10, max_cost=max_cost)
    metadata_path = data / 'metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    report = {
        'metadata': {'seed': metadata.get('seed'), 'date': datetime.now(timezone.utc).isoformat(),
                     'git_revision': revision(), 'model_ids': {n: MODELS[n] for n in names},
                     'max_cost': str(max_cost), 'max_turns': max_turns},
        'split': split, 'limit': limit, 'selected_groups': selected,
        'stopped': None, 'models': {}, 'note_grading': 'Manual checklist in investigator_eval.py; no automatic quality score',
    }
    stopped = False
    for name in names:
        measured = MeasuredClient(client)
        attempted, completed, unchanged, changed = [], 0, 0, []
        notes, fraud_invoices, referrals = [], [], []
        expected, used = Counter(), Counter()
        uncertainty = defaultdict(lambda: Counter({'low': 0, 'medium': 0, 'high': 0}))
        turns, tool_attempts, executed, durations = [], [], [], []
        total_tools, cost = Counter(), Decimal(0)
        for number in selected:
            if stopped:
                break
            group = deepcopy(groups[number])
            before = deepcopy(group)
            counted = CountedTools(tools)
            budget = Budget()
            gateway = Gateway(client=measured, budget=budget, run_id=f'investigate-{name}-{number}',
                              audit_path=audit_path, shared_budgets=(batch,))
            start = perf_counter()
            try:
                result = investigate(by_number[number], group, tools=counted, gateway=gateway,
                                     model_id=MODELS[name], max_turns=max_turns)
            except GatewayBlocked as error:
                if str(error) != 'max_cost_exceeded':
                    raise
                report['stopped'] = 'max_cost_exceeded'
                stopped = True
                # Do not lose already-paid turns when a later call is blocked.
                if budget.calls == 0 and budget.tool_calls == 0:
                    break
                result = Investigation(needs_review=ReviewReferral(number, 'investigation stopped: batch cost limit'))
            attempted.append(number)
            durations.append(perf_counter() - start)
            cost += budget.cost
            turns.append(budget.calls)
            tool_attempts.append(budget.tool_calls)
            executed.append(sum(counted.calls.values()))
            total_tools.update(counted.calls)
            if before == group:
                unchanged += 1
            else:
                changed.append(number)
            for kind in {f.type for f in before} & EXPECTED_TOOLS.keys():
                expected[kind] += 1
                used[kind] += counted.calls[EXPECTED_TOOLS[kind]] > 0
            if result.needs_review:
                referrals.append({'invoice_number': number, 'reason': referral_category(result.needs_review.reason)})
                continue
            completed += 1
            for note in result.notes:
                kind = before[note.finding_index].type
                uncertainty[kind][note.uncertainty] += 1
                notes.append({'invoice_number': number, 'finding_index': note.finding_index,
                              'finding_type': kind, 'summary': note.summary,
                              'related_invoices': list(note.related_invoices), 'uncertainty': note.uncertainty})
                if 'fraud' in note.summary.casefold():
                    fraud_invoices.append(number)
        report['models'][name] = {
            'model_id': MODELS[name], 'attempted': len(attempted), 'completed': completed,
            'completion_rate': ratio(completed, len(attempted)),
            'not_attempted': [n for n in selected if n not in attempted],
            'referrals': {'count': len(referrals), 'by_reason': dict(Counter(r['reason'] for r in referrals)),
                          'invoices': referrals},
            'expected_tool_used': {kind: {'expected': expected[kind], 'used': used[kind],
                                          'rate': ratio(used[kind], expected[kind])} for kind in EXPECTED_TOOLS},
            'findings_unchanged': {'checked': len(attempted), 'unchanged': unchanged,
                                   'rate': ratio(unchanged, len(attempted)), 'changed_groups': changed},
            'fraud_mentions': {'count': len(fraud_invoices), 'invoice_numbers': sorted(set(fraud_invoices))},
            'uncertainty': {kind: dict(counts) for kind, counts in sorted(uncertainty.items())},
            'notes': notes, 'turns': distribution(turns), 'tool_calls': distribution(tool_attempts),
            'executed_tool_calls': distribution(executed), 'tools_executed': dict(total_tools),
            'cost': {'total': str(cost), 'per_investigation': str(cost / len(attempted)) if attempted else None},
            'tokens': {'input': measured.input_tokens, 'output': measured.output_tokens},
            'timing': {'calls': len(measured.durations),
                       'median_seconds': median(measured.durations) if measured.durations else None,
                       'slowest_seconds': max(measured.durations) if measured.durations else None},
            'investigation_timing': distribution(durations),
        }
    report['batch'] = {'calls': batch.calls, 'max_calls': batch.max_calls,
                       'tool_calls': batch.tool_calls, 'max_tool_calls': batch.max_tool_calls,
                       'cost': str(batch.cost), 'max_cost': str(max_cost)}
    return report


def render(report):
    rows = [f"Split: {report['split']} | Groups: {len(report['selected_groups'])} | Stopped: {report['stopped'] or 'no'}",
            'Model | Completed/attempted | Unchanged | Fraud mentions | Turns median/max | Tools median/max | Cost | Cost/investigation']
    for name, model in report['models'].items():
        rows.append(f"{name} | {model['completed']}/{model['attempted']} | {percentage(model['findings_unchanged']['rate'])} | "
                    f"{model['fraud_mentions']['count']} | {model['turns']['median']}/{model['turns']['max']} | "
                    f"{model['tool_calls']['median']}/{model['tool_calls']['max']} | {model['cost']['total']} | "
                    f"{model['cost']['per_investigation'] or 'n/a'}")
        rows.append('  Expected tool used: ' + ', '.join(f"{kind}={percentage(value['rate'])}" for kind, value in model['expected_tool_used'].items()))
        rows.append(f"  Referrals: {model['referrals']['count']} {model['referrals']['by_reason']}")
        rows.append(f"  Uncertainty: {model['uncertainty']}; tokens: {model['tokens']}; timing: {model['timing']}")
    rows.append(f"Batch cost: ${report['batch']['cost']} / ${report['batch']['max_cost']}")
    return '\n'.join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--split', choices=['dev', 'test'], default='dev')
    parser.add_argument('--models', default='nova,sonnet')
    parser.add_argument('--max-cost', required=True)
    parser.add_argument('--max-turns', type=int, default=6)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--json', type=Path, dest='json_path')
    parser.add_argument('--audit', type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('--live is required; no AWS client was created')
    try:
        names, max_cost = model_order(args.models.split(',')), decimal_value(args.max_cost)
        if args.max_turns < 1 or (args.limit is not None and args.limit < 1):
            raise ValueError('--max-turns and --limit must be positive')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        audit = args.audit or Path('runs') / f'investigator-{stamp}.jsonl'
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
                     models=names, limit=args.limit, max_turns=args.max_turns)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(render(report))


if __name__ == '__main__':
    main()
