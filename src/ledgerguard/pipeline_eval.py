"""Budgeted PDF-to-notes evaluation; truth enters only after pipeline execution."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from time import perf_counter

from ledgerguard.evaluate import load_expected, percentage, ratio, revision, score
from ledgerguard.extraction_eval import MODELS
from ledgerguard.gateway import Budget, DEFAULT_PRICES, decimal_value, token_count
from ledgerguard.pdf import load_pdf_inputs
from ledgerguard.pipeline import PipelineModels, run_pipeline
from ledgerguard.rules import ReviewReferral, load_inputs, load_reference, run_rules

STAGES = ('extraction', 'policy', 'investigation')


class StageMeter:
    """Measure provider attempts only; retain no prompts, responses or notes."""
    def __init__(self, client):
        self.client = client
        self.meta = getattr(client, 'meta', None)
        self.stages = {stage: {'calls': 0, 'tokens': {'input': 0, 'output': 0},
                               'seconds': 0.0, 'cost': Decimal(0), 'unpriced_calls': 0}
                       for stage in STAGES}

    @staticmethod
    def classify(request):
        if any('document' in block for message in request.get('messages', [])
               for block in message.get('content', [])):
            return 'extraction'
        names = {tool['toolSpec']['name'] for tool in request['toolConfig']['tools']}
        if 'record_policy_findings' in names:
            return 'policy'
        if 'submit_notes' in names:
            return 'investigation'
        raise ValueError('Unrecognized pipeline request type')

    def converse(self, **request):
        stage = self.stages[self.classify(request)]
        stage['calls'] += 1
        start = perf_counter()
        try:
            response = self.client.converse(**request)
            try:
                usage = response['usage']
                inputs = token_count(usage['inputTokens'])
                outputs = token_count(usage['outputTokens'])
                if usage.get('cacheReadInputTokens', 0) or usage.get('cacheWriteInputTokens', 0):
                    raise ValueError('Unsupported usage')
                cost = DEFAULT_PRICES[request['modelId']].cost(inputs, outputs)
            except (KeyError, TypeError, ValueError):
                stage['unpriced_calls'] += 1
            else:
                stage['tokens']['input'] += inputs
                stage['tokens']['output'] += outputs
                stage['cost'] += cost
            return response
        finally:
            stage['seconds'] += perf_counter() - start


def compare(data: Path, *, client, max_cost: Decimal, audit_path: Path,
            split='dev', limit=None, models: PipelineModels = PipelineModels()) -> dict:
    """Score the entire selected cohort, including unattempted/referral documents.

    PDF filenames supply trusted document identity for scoring. Extracted invoice
    numbers remain untouched inside the pipeline and are scored separately.
    Cost/time per invoice use selected documents, including incomplete runs.
    """
    if split not in {'dev', 'test'} or (limit is not None and (type(limit) is not int or limit < 1)):
        raise ValueError('Use split dev/test and a positive limit')
    if any(model not in MODELS.values() for model in (models.extract, models.policy, models.investigate)):
        raise ValueError('Models must be nova or sonnet profiles')
    data, max_cost = Path(data), decimal_value(max_cost)
    pdfs = load_pdf_inputs(data)
    splits = json.loads((data / 'splits.json').read_text())
    if set(pdfs) != set(splits):
        raise ValueError('PDFs and splits.json must have matching document IDs')
    selected = sorted(document_id for document_id in pdfs if splits[document_id] == split)
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError('No PDFs in selected split')
    reference = load_reference(data)
    batch = Budget(label='batch', max_calls=len(selected) * 20, max_tool_calls=len(selected) * 10,
                   max_cost=max_cost)
    meter = StageMeter(client)
    start = perf_counter()
    result = run_pipeline({n: pdfs[n] for n in selected}, reference, client=meter,
                          audit_path=audit_path, models=models, batch=batch)
    elapsed = perf_counter() - start

    # Ground truth and answers must not be loaded until the pipeline returns.
    inputs = load_inputs(data)
    truth = {i.invoice_number: i for i in inputs.invoices}
    if len(truth) != len(inputs.invoices) or set(truth) != set(pdfs):
        raise ValueError('PDFs and ground-truth invoices must have matching unique IDs')
    scope = {n: split for n in selected}
    selected_set = set(selected)
    answers = [a for a in load_expected(data) if set(a['invoice_numbers']) <= selected_set]
    baseline_rules = run_rules([truth[n] for n in selected], reference.contracts,
                               reference.history, reference.policy_clauses)
    baseline = score(baseline_rules.findings, answers, scope, split, baseline_rules.needs_review)
    number_to_document = {i.invoice_number: n for n, i in result.invoices.items()}
    scored_findings = [f.model_copy(update={'invoice_numbers': [number_to_document[n]
                        for n in f.invoice_numbers]}) for f in result.findings]
    referrals = [ReviewReferral(r.get('document_id') or number_to_document.get(r.get('invoice_number')),
                               r['reason']) for r in result.referrals]
    end_to_end = score(scored_findings, answers, scope, split, referrals)
    exact = sum(invoice == truth[n] for n, invoice in result.invoices.items())
    number_mismatches = [n for n, invoice in result.invoices.items()
                         if invoice.invoice_number != truth[n].invoice_number]
    groups = {f.invoice_numbers[0] for f in result.findings}
    calls = max((b['calls'] for b in result.budgets.values()), default=0)
    cost = max((b['cost'] for b in result.budgets.values()), default=Decimal(0))
    stage_cost = sum((s['cost'] for s in meter.stages.values()), Decimal(0))
    stage_report = {name: {**values, 'cost': str(values['cost'])}
                    for name, values in meter.stages.items()}
    metadata_path = data / 'metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    return {
        'pipeline_run_id': result.pipeline_run_id,
        'metadata': {'seed': metadata.get('seed'), 'date': datetime.now(timezone.utc).isoformat(),
                     'git_revision': revision(), 'model_ids': {'extract': models.extract,
                     'policy': models.policy, 'investigate': models.investigate}, 'max_cost': str(max_cost)},
        'split': split, 'limit': limit, 'selected_documents': selected,
        'scoring_scope': 'All selected documents; only complete selected answer groups',
        'scoring_identity': 'PDF filename; extracted invoice numbers remain unchanged in pipeline results',
        'baseline': baseline, 'end_to_end': end_to_end,
        'extraction': {'selected': len(selected), 'successful': len(result.invoices),
                       'exact_matches': exact, 'accuracy': ratio(exact, len(selected)),
                       'invoice_number_mismatches': number_mismatches,
                       'invoice_number_mismatch_count': len(number_mismatches)},
        'referrals': {'count': len(result.referrals), 'by_stage': {
            stage: sum(r['stage'] == stage for r in result.referrals)
            for stage in ('extraction', 'rules', 'policy', 'investigation', 'budget')},
            'items': result.referrals},
        'investigation': {'groups': len(groups), 'completed': len(result.notes),
                           'completion_rate': ratio(len(result.notes), len(groups))},
        'largest_run': {'calls': calls, 'cost': str(cost),
                        'within_limits': calls <= 20 and cost <= Decimal('0.50')},
        'budgets': {n: {'calls': b['calls'], 'cost': str(b['cost'])} for n, b in result.budgets.items()},
        'batch': {'calls': batch.calls, 'tool_calls': batch.tool_calls, 'cost': str(batch.cost),
                  'max_calls': batch.max_calls, 'max_tool_calls': batch.max_tool_calls,
                  'max_cost': str(batch.max_cost)},
        'cost': {'total': str(batch.cost), 'per_invoice': str(batch.cost / len(selected)),
                 'stage_total': str(stage_cost), 'stages_reconcile': stage_cost == batch.cost,
                 'unattributed': str(batch.cost - stage_cost)},
        'timing': {'total_seconds': elapsed, 'per_invoice_seconds': elapsed / len(selected),
                   'basis': 'Pipeline wall time; per-stage seconds measure provider calls only'},
        'stages': stage_report,
        'stopped': result.stopped, 'skipped_stages': result.skipped_stages,
        'findings': [f.model_dump(mode='json') for f in result.findings],
        'notes': {number: [note.model_dump(mode='json') for note in notes]
                  for number, notes in result.notes.items()},
        'note_grading': 'Manual five-point checklist in investigator_eval.py; counts are not quality scores',
    }


def render(report):
    rows = [f"Split: {report['split']} | Stopped: {report['stopped'] or 'no'}",
            f"Skipped/unfinished stages: {', '.join(report['skipped_stages']) or 'none'}"]
    for label, scored in [('Rules baseline', report['baseline']), ('End to end', report['end_to_end'])]:
        overall, clean = scored['overall'], scored['clean_invoices']
        rows.append(f"{label}: {overall['caught']}/{overall['expected']} | "
                    f"recall {percentage(overall['recall'])} | precision {percentage(overall['precision'])} | "
                    f"clean flagged {clean['wrongly_flagged']}/{clean['total']}")
    rows.extend(f"  {kind}: {counts['caught']}/{counts['expected']}"
                for kind, counts in report['end_to_end']['by_type'].items())
    extraction, investigation = report['extraction'], report['investigation']
    rows += [f"Extraction exact: {extraction['exact_matches']}/{extraction['selected']} "
             f"({percentage(extraction['accuracy'])}); invoice-number mismatches: "
             f"{extraction['invoice_number_mismatch_count']}",
             f"Investigation: {investigation['completed']}/{investigation['groups']} groups; "
             f"notes: {sum(len(notes) for notes in report['notes'].values())}",
             f"Referrals: {report['referrals']['count']} {report['referrals']['by_stage']}",
             f"Largest run: {report['largest_run']['calls']} calls, ${report['largest_run']['cost']}; "
             f"within limits: {report['largest_run']['within_limits']}",
             f"Total: ${report['cost']['total']}, {report['timing']['total_seconds']:.3f}s; "
             f"per selected invoice: ${report['cost']['per_invoice']}, "
             f"{report['timing']['per_invoice_seconds']:.3f}s"]
    for name, stage in report['stages'].items():
        rows.append(f"  {name}: {stage['calls']} calls, tokens {stage['tokens']}, "
                    f"${stage['cost']}, {stage['seconds']:.3f}s")
    rows.append(f"Stage costs reconcile: {report['cost']['stages_reconcile']}; "
                f"unattributed: ${report['cost']['unattributed']}")
    return '\n'.join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--split', choices=['dev', 'test'], default='dev')
    parser.add_argument('--max-cost', required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--json', type=Path, dest='json_path')
    parser.add_argument('--audit', type=Path)
    for stage, default in [('extract', 'nova'), ('policy', 'nova'), ('investigate', 'sonnet')]:
        parser.add_argument(f'--{stage}-model', choices=MODELS, default=default)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('--live is required; no AWS client was created')
    try:
        max_cost = decimal_value(args.max_cost)
        if args.limit is not None and args.limit < 1:
            raise ValueError('--limit must be positive')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        audit = args.audit or Path('runs') / f'pipeline-{stamp}.jsonl'
        if 'data' in audit.resolve().parts or audit.resolve().is_relative_to(args.data.resolve()):
            raise ValueError('Audit log must be outside input/data directories')
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
                     limit=args.limit, models=PipelineModels(MODELS[args.extract_model],
                     MODELS[args.policy_model], MODELS[args.investigate_model]))
    if args.json_path:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(render(report))


if __name__ == '__main__':
    main()
