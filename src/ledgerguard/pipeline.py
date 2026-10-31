"""PDF-only orchestration with one governed run per document; no live setup."""
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from ledgerguard.extractor import extract
from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, MODEL_ID, NOVA_MODEL_ID
from ledgerguard.investigator import Note, investigate
from ledgerguard.models import Finding, Invoice
from ledgerguard.policy import check_policy
from ledgerguard.rules import ReferenceInputs, run_rules
from ledgerguard.tools import ReadOnlyTools

NOVA = NOVA_MODEL_ID
SONNET = MODEL_ID
STAGES = ('extraction', 'rules', 'policy', 'investigation')


@dataclass(frozen=True)
class PipelineModels:
    extract: str = NOVA
    policy: str = NOVA
    investigate: str = SONNET


@dataclass
class PipelineResult:
    invoices: dict[str, Invoice] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    notes: dict[str, list[Note]] = field(default_factory=dict)
    referrals: list[dict[str, str]] = field(default_factory=list)
    budgets: dict[str, dict] = field(default_factory=dict)
    stopped: str | None = None
    # On a stop, includes the unfinished current stage and all later stages.
    skipped_stages: list[str] = field(default_factory=list)


def run_pipeline(pdfs: dict[str, bytes], reference: ReferenceInputs, *, client,
                 audit_path: Path, models: PipelineModels, batch: Budget) -> PipelineResult:
    """Run extraction, batch rules, policy, then grouped investigation.

    Input order determines which repeated invoice number is retained. A batch
    block returns partial results immediately; invoice blocks skip further model
    work for that document while preserving already produced evidence/findings.
    Audit I/O failures propagate rather than silently continuing without logs.
    """
    if batch.label is None:
        batch.label = 'batch'
    if batch.label != 'batch':
        raise ValueError('Shared pipeline budget must have label batch')
    result = PipelineResult()
    budgets = {document_id: Budget(label='invoice') for document_id in pdfs}
    gateways = {document_id: Gateway(client=client, budget=budget,
                                    run_id=f'run-{document_id}', audit_path=audit_path,
                                    shared_budgets=(batch,))
                for document_id, budget in budgets.items()}
    by_number, exhausted = {}, set()

    def finish():
        result.budgets = {document_id: {'calls': budget.calls, 'cost': budget.cost}
                          for document_id, budget in budgets.items()}
        return result

    def blocked(error, document_id, stage):
        if error.scope == 'batch':
            result.stopped = str(error)
            result.skipped_stages = list(STAGES[STAGES.index(stage):])
            return True
        if error.scope != 'invoice':
            raise error
        exhausted.add(document_id)
        result.referrals.append({'document_id': document_id, 'stage': 'budget', 'reason': str(error)})
        return False

    for document_id, pdf in pdfs.items():
        try:
            extraction = extract(pdf, gateway=gateways[document_id], model_id=models.extract)
        except GatewayBlocked as error:
            if blocked(error, document_id, 'extraction'):
                return finish()
            continue
        if extraction.needs_review:
            result.referrals.append({'document_id': document_id, 'stage': 'extraction',
                                     'reason': extraction.needs_review.reason})
            continue
        invoice = extraction.invoice
        if invoice.invoice_number in by_number:
            result.referrals.append({'document_id': document_id, 'stage': 'extraction',
                                     'reason': 'duplicate invoice number in batch'})
            continue
        result.invoices[document_id] = invoice
        by_number[invoice.invoice_number] = document_id

    rules = run_rules(list(result.invoices.values()), reference.contracts,
                      reference.history, reference.policy_clauses)
    result.findings.extend(rules.findings)
    result.referrals.extend({'invoice_number': referral.invoice_number, 'stage': 'rules',
                             'reason': referral.reason} for referral in rules.needs_review)

    for document_id, invoice in result.invoices.items():
        if document_id in exhausted:
            continue
        try:
            policy = check_policy(invoice, reference.policy_clauses,
                                  gateway=gateways[document_id], model_id=models.policy)
        except GatewayBlocked as error:
            if blocked(error, document_id, 'policy'):
                return finish()
            continue
        result.findings.extend(policy.findings)
        result.referrals.extend({'invoice_number': referral.invoice_number, 'stage': 'policy',
                                 'reason': referral.reason} for referral in policy.needs_review)

    groups = defaultdict(list)
    for finding in result.findings:
        groups[finding.invoice_numbers[0]].append(finding)
    tools = ReadOnlyTools(list(result.invoices.values()), reference.history)
    for number, findings in groups.items():
        document_id = by_number[number]
        if document_id in exhausted:
            continue
        try:
            investigation = investigate(result.invoices[document_id], deepcopy(findings), tools=tools,
                                        gateway=gateways[document_id], model_id=models.investigate)
        except GatewayBlocked as error:
            if blocked(error, document_id, 'investigation'):
                return finish()
            continue
        if investigation.needs_review:
            result.referrals.append({'invoice_number': number, 'stage': 'investigation',
                                     'reason': investigation.needs_review.reason})
        else:
            result.notes[number] = investigation.notes
    return finish()
