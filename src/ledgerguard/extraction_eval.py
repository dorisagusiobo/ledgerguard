"""Explicitly budgeted live PDF extraction comparison; core supports fake clients."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from time import perf_counter

from ledgerguard.evaluate import load_expected, percentage, ratio, revision, score
from ledgerguard.extractor import extract
from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, MODEL_ID, NOVA_MODEL_ID, decimal_value
from ledgerguard.pdf import line_item_source_text, load_pdf_inputs
from ledgerguard.rules import ReviewReferral, load_inputs, run_rules

MODELS = {'nova': NOVA_MODEL_ID, 'sonnet': MODEL_ID}
HEADERS = ('invoice_number', 'vendor_name', 'po_number', 'invoice_date', 'due_date',
           'currency', 'subtotal', 'tax', 'total')
LINE_FIELDS = ('description', 'quantity', 'unit_price', 'amount')


class MeasuredClient:
    """Capture only counts and duration; never retain response text or requests."""
    def __init__(self, client):
        self.client = client
        self.meta = getattr(client, 'meta', None)
        self.durations = []
        self.input_tokens = self.output_tokens = 0

    def converse(self, **kwargs):
        start = perf_counter()
        try:
            response = self.client.converse(**kwargs)
            usage = response.get('usage', {}) if isinstance(response, dict) else {}
            for name, attribute in [('inputTokens', 'input_tokens'), ('outputTokens', 'output_tokens')]:
                value = usage.get(name) if isinstance(usage, dict) else None
                if type(value) is int and value >= 0:
                    setattr(self, attribute, getattr(self, attribute) + value)
            return response
        finally:
            self.durations.append(perf_counter() - start)


def model_order(models):
    names = list(models)
    if not names or len(set(names)) != len(names) or any(name not in MODELS for name in names):
        raise ValueError('Models must be a nonempty, unique list of nova,sonnet')
    return [name for name in MODELS if name in names]


def compare(data: Path, *, client, max_cost: Decimal, audit_path: Path,
            split='dev', models=('nova', 'sonnet'), limit=None) -> dict:
    """Compare only the selected split, with one shared batch spending ceiling.

    Field/whole-invoice denominators are attempted invoices (including referrals).
    Downstream scoring covers all selected invoices, including unattempted ones,
    so a stopped run cannot claim full-batch recall. Limited scopes include only
    answer groups whose complete set of invoice IDs is selected.
    """
    data = Path(data)
    if split not in {'dev', 'test'} or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('Use split dev/test and a positive limit')
    names = model_order(models)
    max_cost = decimal_value(max_cost)
    pdfs = load_pdf_inputs(data)
    inputs = load_inputs(data)  # Scoring only: never passed to extract/client.
    splits = json.loads((data / 'splits.json').read_text())
    truth = {i.invoice_number: i for i in inputs.invoices}
    if set(truth) != set(splits) or set(truth) != set(pdfs):
        raise ValueError('PDFs, JSON invoices and splits.json must have matching document IDs')
    selected = sorted(number for number in truth if splits[number] == split)
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError('No invoices in selected split')
    scope = {number: split for number in selected}
    selected_set = set(selected)
    answers = [a for a in load_expected(data) if set(a['invoice_numbers']) <= selected_set]
    arithmetic = {number for a in answers if a['type'] == 'arithmetic_error' for number in a['invoice_numbers']}
    baseline_result = run_rules([truth[n] for n in selected], inputs.contracts, inputs.history, inputs.policy_clauses)
    baseline = score(baseline_result.findings, answers, scope, split, baseline_result.needs_review)
    batch = Budget(max_calls=len(selected) * len(names) * Budget().max_calls, max_cost=max_cost)
    metadata_path = data / 'metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    report = {'metadata': {'seed': metadata.get('seed'), 'date': datetime.now(timezone.utc).isoformat(),
                           'git_revision': revision(), 'model_ids': {n: MODELS[n] for n in names},
                           'max_cost': str(max_cost)},
              'split': split, 'limit': limit, 'selected_invoices': selected,
              'stopped': None, 'baseline': baseline, 'models': {},
              'downstream_identity': 'Trusted PDF filename; extracted invoice number scored separately',
              'downstream_scope': 'All selected invoices, including referrals and unattempted invoices'}
    stopped = False
    for name in names:
        measured = MeasuredClient(client)
        extracted, referrals, mismatches, evaluated = [], [], [], []
        counts = Counter()
        line_correct = source_correct = whole_correct = preserved = preservation_total = 0
        cost = Decimal(0)
        for number in selected:
            if stopped:
                break
            budget = Budget()
            gateway = Gateway(client=measured, budget=budget, run_id=f'extract-{name}-{number}',
                              audit_path=audit_path, shared_budgets=(batch,))
            try:
                result = extract(pdfs[number], gateway=gateway, model_id=MODELS[name])
            except GatewayBlocked as error:
                if str(error) != 'max_cost_exceeded':
                    raise
                report['stopped'] = 'max_cost_exceeded'
                stopped = True
                break
            evaluated.append(number)
            cost += budget.cost
            if number in arithmetic:
                preservation_total += 1
            if result.needs_review:
                # Keep trusted filename identity; group safe, fixed reason prefixes.
                reason = result.needs_review.reason.split(':', 1)[0]
                referrals.append(ReviewReferral(number, reason))
                continue
            invoice, expected = result.invoice, truth[number]
            for field in HEADERS:
                if getattr(invoice, field) == getattr(expected, field):
                    counts[field] += 1
                else:
                    mismatches.append({'invoice_number': number, 'field': field})
            lines_match = len(invoice.line_items) == len(expected.line_items) and all(
                all(getattr(a, field) == getattr(b, field) for field in LINE_FIELDS)
                for a, b in zip(invoice.line_items, expected.line_items))
            sources_match = len(invoice.line_items) == len(expected.line_items) and all(
                a.source.page == 1 and a.source.text == line_item_source_text(
                    b.description, b.quantity, b.unit_price, b.amount)
                for a, b in zip(invoice.line_items, expected.line_items))
            line_correct += lines_match
            source_correct += sources_match
            whole_correct += invoice == expected
            if not lines_match:
                mismatches.append({'invoice_number': number, 'field': 'line_items'})
            if not sources_match:
                mismatches.append({'invoice_number': number, 'field': 'line_items.source'})
            if number in arithmetic and invoice.total == expected.total:
                preserved += 1
            # Use document identity for finding attribution; never let an incorrect
            # extracted invoice number merge documents or crash the scorer.
            extracted.append(invoice.model_copy(update={'invoice_number': number}))
        detected = run_rules(extracted, inputs.contracts, inputs.history, inputs.policy_clauses)
        downstream = score(detected.findings, answers, scope, split,
                           [*referrals, *detected.needs_review])
        attempted = len(evaluated)
        report['models'][name] = {
            'model_id': MODELS[name], 'selected': len(selected), 'attempted': attempted,
            'successful': len(extracted), 'not_attempted': [n for n in selected if n not in evaluated],
            'referrals': {'count': len(referrals), 'by_reason': dict(Counter(r.reason for r in referrals)),
                          'invoices': [{'invoice_number': r.invoice_number, 'reason': r.reason} for r in referrals]},
            'accuracy_denominator': attempted,
            'field_accuracy': {field: ratio(counts[field], attempted) for field in HEADERS},
            'line_items_accuracy': ratio(line_correct, attempted),
            'source_accuracy': ratio(source_correct, attempted),
            'whole_invoice_accuracy': ratio(whole_correct, attempted),
            'error_preservation': {'expected': preservation_total, 'preserved': preserved,
                                   'accuracy': ratio(preserved, preservation_total)},
            'mismatches': mismatches, 'downstream': downstream,
            'cost': {'total': str(cost), 'per_invoice': str(cost / attempted) if attempted else None},
            'tokens': {'input': measured.input_tokens, 'output': measured.output_tokens},
            'timing': {'calls': len(measured.durations),
                       'median_seconds': median(measured.durations) if measured.durations else None,
                       'slowest_seconds': max(measured.durations) if measured.durations else None},
        }
    report['batch'] = {'calls': batch.calls, 'cost': str(batch.cost), 'max_cost': str(max_cost)}
    return report


def render(report):
    rows = [f"Split: {report['split']} | Stopped: {report['stopped'] or 'no'}",
            'Model | Attempted | Referrals | Whole invoice | Recall | Precision | Clean flagged | Cost | Cost/invoice']
    def row(name, model, detection, cost, per_invoice):
        clean = detection['clean_invoices']
        return (f"{name} | {model.get('attempted', '-')} | {model.get('referrals', {}).get('count', '-')} | "
                f"{percentage(model.get('whole_invoice_accuracy'))} | {percentage(detection['overall']['recall'])} | "
                f"{percentage(detection['overall']['precision'])} | {clean['wrongly_flagged']}/{clean['total']} | {cost} | {per_invoice}")
    rows.append(row('truth baseline', {}, report['baseline'], '-', '-'))
    for name, model in report['models'].items():
        rows.append(row(name, model, model['downstream'], model['cost']['total'], model['cost']['per_invoice'] or 'n/a'))
        rows.append('  Fields: ' + ', '.join(f'{key}={percentage(value)}' for key, value in model['field_accuracy'].items()))
        rows.append(f"  Lines: {percentage(model['line_items_accuracy'])}; sources: {percentage(model['source_accuracy'])}; "
                    f"error preservation: {percentage(model['error_preservation']['accuracy'])}")
        rows.append(f"  Tokens: {model['tokens']}; timing: {model['timing']}; referrals: {model['referrals']['by_reason']}")
        rows.append('  Mismatches: ' + ('; '.join(f"{m['invoice_number']} {m['field']}" for m in model['mismatches']) or 'none'))
    rows.append(f"Batch cost: ${report['batch']['cost']} / ${report['batch']['max_cost']}")
    return '\n'.join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--split', choices=['dev', 'test'], default='dev')
    parser.add_argument('--models', default='nova,sonnet')
    parser.add_argument('--max-cost', required=True)
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
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive')
        if not (args.data / 'invoices_pdf').is_dir():
            raise ValueError('Missing invoices_pdf; regenerate into a fresh output directory first')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        audit = args.audit or Path('runs') / f'extraction-{stamp}.jsonl'
        if 'data' in audit.resolve().parts:
            raise ValueError('Audit log must be outside data directories')
        if args.json_path and (args.json_path.resolve().is_relative_to(args.data.resolve())
                               or args.json_path.resolve() == audit.resolve()):
            raise ValueError('JSON report must be outside inputs and separate from audit log')
    except (ValueError, ArithmeticError) as error:
        parser.error(str(error))
    # Creating a real client is confined to this explicit live-only entry point.
    import boto3
    from botocore.config import Config
    client = boto3.client('bedrock-runtime', region_name='us-east-1',
                          config=Config(retries={'total_max_attempts': 1}))
    report = compare(args.data, client=client, max_cost=max_cost, audit_path=audit,
                     split=args.split, models=names, limit=args.limit)
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(render(report))


if __name__ == '__main__':
    main()
