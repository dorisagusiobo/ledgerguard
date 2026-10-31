"""Atomic persistence of completed/partial pipeline results, without PDF bytes."""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ledgerguard.gateway import decimal_value
from ledgerguard.investigator import Note
from ledgerguard.models import Finding
from ledgerguard.pipeline import PipelineResult


@dataclass(frozen=True)
class RunMetadata:
    run_id: str
    started_at: datetime
    finished_at: datetime
    model_ids: dict[str, str]
    document_hashes: dict[str, str]
    git_revision: str | None = None
    # Optional import of existing JSONL records. Gateway writes remain JSONL in 4a.
    audit_records: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class StoredFinding:
    finding_id: int
    finding: Finding
    notes: list[Note]


AUDIT_FIELDS = ('time', 'run_id', 'model', 'decision', 'outcome', 'reason', 'scope',
                'input_tokens', 'output_tokens', 'cost', 'reserved_cost', 'call_id',
                'tool', 'phase', 'row_count')


def append_audit(conn, record: Mapping, *, pipeline_run_id: str | None = None):
    """Append a known gateway record using the caller's transaction; never upsert."""
    if set(record) - set(AUDIT_FIELDS):
        raise ValueError('Unrecognized audit fields')
    values = dict(record)
    for name in ('cost', 'reserved_cost'):
        if values.get(name) is not None:
            values[name] = decimal_value(values[name])
    conn.execute('''INSERT INTO audit_log (pipeline_run_id, time, run_id, model, decision,
        outcome, reason, scope, input_tokens, output_tokens, cost, reserved_cost,
        call_id, tool, phase, row_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                 (pipeline_run_id, *(values.get(name) for name in AUDIT_FIELDS)))


def save_run(conn, result: PipelineResult, metadata: RunMetadata | Mapping):
    """Commit one result in full, or roll it all back. Requires an idle connection.

    document_hashes includes every submitted PDF, including failed extractions.
    Notes use the investigator's index within each first-invoice finding group.
    Duplicate run IDs fail rather than replacing prior evidence or audit records.
    """
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise ValueError('Saving a run requires an idle connection')
    if isinstance(metadata, Mapping):
        metadata = RunMetadata(**metadata)
    needed = set(result.invoices) | set(result.budgets) | {
        r['document_id'] for r in result.referrals if 'document_id' in r}
    if not needed <= metadata.document_hashes.keys():
        raise ValueError('Missing document hashes for pipeline inputs')
    budget_data = {doc: {'calls': b['calls'], 'cost': str(decimal_value(b['cost']))}
                   for doc, b in result.budgets.items()}
    status = 'stopped' if result.stopped else 'needs_review' if result.referrals else 'completed'
    with conn.transaction():
        conn.execute('''INSERT INTO runs (run_id, started_at, finished_at, model_ids,
            git_revision, status, stopped_reason, skipped_stages, budgets)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                     (metadata.run_id, metadata.started_at, metadata.finished_at,
                      Jsonb(metadata.model_ids), metadata.git_revision, status, result.stopped,
                      result.skipped_stages, Jsonb(budget_data)))
        for document_id, digest in metadata.document_hashes.items():
            conn.execute('INSERT INTO documents (run_id, document_id, pdf_sha256) VALUES (%s, %s, %s)',
                         (metadata.run_id, document_id, digest))
        for document_id, invoice in result.invoices.items():
            invoice_id = conn.execute('''INSERT INTO invoices (run_id, document_id, invoice_number,
                vendor_name, po_number, invoice_date, due_date, currency, subtotal, tax, total)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING invoice_id''',
                (metadata.run_id, document_id, invoice.invoice_number, invoice.vendor_name,
                 invoice.po_number, invoice.invoice_date, invoice.due_date, invoice.currency,
                 invoice.subtotal, invoice.tax, invoice.total)).fetchone()[0]
            for position, item in enumerate(invoice.line_items):
                conn.execute('''INSERT INTO line_items (invoice_id, position, description, quantity,
                    unit_price, amount, source_page, source_text) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                    (invoice_id, position, item.description, item.quantity, item.unit_price,
                     item.amount, item.source.page, item.source.text))
        groups = defaultdict(list)
        for position, finding in enumerate(result.findings):
            finding_id = conn.execute('''INSERT INTO findings (run_id, position, type, invoice_numbers,
                amount_at_issue, calculation, evidence, citation, no_clause_reason, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING finding_id''',
                (metadata.run_id, position, finding.type, finding.invoice_numbers, finding.amount_at_issue,
                 finding.calculation, Jsonb([s.model_dump(mode='json') for s in finding.evidence]),
                 Jsonb(finding.citation.model_dump(mode='json')) if finding.citation else None,
                 finding.no_clause_reason, finding.confidence)).fetchone()[0]
            groups[finding.invoice_numbers[0]].append(finding_id)
        for number, notes in result.notes.items():
            if number not in groups or sorted(n.finding_index for n in notes) != list(range(len(groups[number]))):
                raise ValueError('Notes must match every finding index in their group')
            for note in notes:
                conn.execute('''INSERT INTO notes (finding_id, finding_index, summary, related_invoices,
                    uncertainty) VALUES (%s, %s, %s, %s, %s)''',
                    (groups[number][note.finding_index], note.finding_index, note.summary,
                     note.related_invoices, note.uncertainty))
        for referral in result.referrals:
            conn.execute('''INSERT INTO referrals (run_id, document_id, invoice_number, stage, reason)
                VALUES (%s, %s, %s, %s, %s)''', (metadata.run_id, referral.get('document_id'),
                referral.get('invoice_number'), referral['stage'], referral['reason']))
        for record in metadata.audit_records:
            append_audit(conn, record, pipeline_run_id=metadata.run_id)


def load_findings(conn, run_id: str) -> list[StoredFinding]:
    """Return ordered review entries, with original evidence, clauses and notes.

    NUMERIC values are passed directly from psycopg into the Finding schema;
    there is no JSON or float conversion of money.
    """
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute('''SELECT f.*, review.status, n.finding_index, n.summary, n.related_invoices, n.uncertainty
            FROM findings f JOIN finding_review_status review USING (finding_id)
            LEFT JOIN notes n USING (finding_id)
            WHERE f.run_id = %s ORDER BY f.position''', (run_id,))
        rows = cursor.fetchall()
    entries = []
    for row in rows:
        finding = Finding(**{key: row[key] for key in Finding.model_fields})
        if not isinstance(finding.amount_at_issue, Decimal):
            raise TypeError('Database money must be Decimal')
        notes = [] if row['summary'] is None else [Note(**{key: row[key] for key in Note.model_fields})]
        entries.append(StoredFinding(row['finding_id'], finding, notes))
    return entries
