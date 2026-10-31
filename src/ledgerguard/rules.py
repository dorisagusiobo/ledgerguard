"""Pure USD audit rules and a separate, answer-key-blind input loader."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re

from pydantic import Field

from ledgerguard.models import Citation, Finding, Invoice, Money, Quantity, Schema

CENT = Decimal('0.01')


class RuleConfig(Schema):
    po_required_over: Money = Decimal('1000')
    split_limit: Money = Decimal('5000')
    split_window_days: int = Field(default=7, ge=0, strict=True)
    unusual_multiplier: Quantity = Decimal('3')


@dataclass
class ReferenceInputs:
    contracts: list[dict]
    history: list[dict]
    policy_clauses: dict[str, Citation]


@dataclass
class RuleInputs:
    invoices: list[Invoice]
    contracts: list[dict]
    history: list[dict]
    policy_clauses: dict[str, Citation]


@dataclass
class ReviewReferral:
    invoice_number: str
    reason: str


@dataclass
class RuleResult:
    findings: list[Finding]
    needs_review: list[ReviewReferral]


def load_reference(path: Path) -> ReferenceInputs:
    """Read contracts, history and policy only; no invoices or evaluation answers."""
    path = Path(path)
    policy = (path / 'policy.md').read_text()
    title = next(line.removeprefix('# ').strip() for line in policy.splitlines() if line.startswith('# '))
    clauses = {}
    for line in policy.splitlines():
        match = re.match(r'(POL-\d+):', line)
        if match:
            key = match.group(1)
            clauses[key] = Citation(document='policy.md', version=title,
                                    clause_id=key, quote=line)
    return ReferenceInputs(
        contracts=json.loads((path / 'contracts.json').read_text()),
        history=json.loads((path / 'history.json').read_text()),
        policy_clauses=clauses,
    )


def load_inputs(path: Path) -> RuleInputs:
    """Load reference data and JSON invoices for existing offline rule consumers."""
    reference = load_reference(path)
    return RuleInputs(contracts=reference.contracts, history=reference.history,
                      policy_clauses=reference.policy_clauses,
                      invoices=[Invoice.model_validate_json(p.read_text())
                                for p in sorted((Path(path) / 'invoices').glob('*.json'))])


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def finding(kind, invoices, amount, calculation, citation=None, confidence=1.0):
    return Finding(type=kind, invoice_numbers=sorted(i.invoice_number for i in invoices),
                   amount_at_issue=cents(amount), calculation=calculation,
                   evidence=[item.source for i in sorted(invoices, key=lambda i: i.invoice_number)
                             for item in i.line_items], citation=citation,
                   no_clause_reason='no applicable clause identified' if citation is None else None,
                   confidence=confidence)


def arithmetic_checks(invoices: list[Invoice]) -> list[Finding]:
    """Report one finding per invoice, listing every failed equation."""
    results = []
    for invoice in invoices:
        checks = [(f'line {n}: quantity × unit price', cents(item.quantity * item.unit_price), item.amount)
                  for n, item in enumerate(invoice.line_items, 1)]
        checks += [('line sum vs subtotal', sum((i.amount for i in invoice.line_items), Decimal(0)), invoice.subtotal),
                   ('subtotal + tax vs total', invoice.subtotal + invoice.tax, invoice.total)]
        failures = [f'{label}: |{expected} - {actual}| = {abs(expected - actual)}'
                    for label, expected, actual in checks if expected != actual]
        if failures:
            correct_total = cents(sum((i.quantity * i.unit_price for i in invoice.line_items), Decimal(0)) + invoice.tax)
            amount = abs(invoice.total - correct_total)
            failures.append(f'Amount at issue: |stated total {invoice.total} - correct total {correct_total}| = {amount}')
            results.append(finding('arithmetic_error', [invoice], amount, '; '.join(failures)))
    return results


def duplicate_key(invoice):
    # Source text is evidence, not invoice identity; line order is irrelevant.
    items = tuple(sorted((i.description, i.quantity, i.unit_price, i.amount) for i in invoice.line_items))
    return (invoice.vendor_name, invoice.currency, invoice.invoice_date, invoice.po_number, items)


def duplicate_checks(invoices: list[Invoice]) -> list[Finding]:
    groups = defaultdict(list)
    for invoice in invoices:
        groups[duplicate_key(invoice)].append(invoice)
    results = []
    for group in groups.values():
        ordered = sorted(group, key=lambda i: i.invoice_number)
        for duplicate in ordered[1:]:
            results.append(finding('duplicate', [ordered[0], duplicate], duplicate.total,
                                   f'Duplicate of {ordered[0].invoice_number}: total {duplicate.total}'))
    return results


def contract_checks(invoices: list[Invoice], contracts: list[dict]) -> list[Finding]:
    results = []
    for invoice in invoices:
        groups = defaultdict(list)
        for item in invoice.line_items:
            matches = [c for c in contracts if c['vendor_name'] == invoice.vendor_name
                       and c['item'] == item.description
                       and date.fromisoformat(c['effective_from']) <= invoice.invoice_date
                       <= date.fromisoformat(c['effective_to'])]
            if not matches:
                continue
            # Prefer the latest effective rate, with stable ordering on ties.
            contract = max(matches, key=lambda c: (c['effective_from'], c['version'], c['clause_id']))
            rate = Decimal(contract['unit_price'])
            if item.unit_price > rate:
                amount = cents((item.unit_price - rate) * item.quantity)
                if amount == 0:
                    continue
                citation = Citation(document='contracts.json', version=contract['version'],
                                    clause_id=contract['clause_id'],
                                    quote=f"{contract['item']}: ${contract['unit_price']} per unit")
                calculation = f'{item.description}: ({item.unit_price} - {rate}) × {item.quantity} = {amount}'
                groups[(citation.clause_id, citation.version)].append((amount, calculation, citation))
        for (clause_id, version), lines in sorted(groups.items()):
            # Never combine distinct clause IDs or versions into a fabricated ID.
            citation = Citation(document='contracts.json', version=version, clause_id=clause_id,
                                quote='\n'.join(sorted({line[2].quote for line in lines})))
            total = sum((line[0] for line in lines), Decimal(0))
            calculation = '; '.join(sorted(line[1] for line in lines)) + f'; Total overcharge = {total}'
            results.append(finding('contract_overcharge', [invoice], total, calculation, citation))
    return results


def po_checks(invoices: list[Invoice], clauses: dict[str, Citation], config: RuleConfig) -> list[Finding]:
    return [finding('policy_violation', [i], i.total,
                    f'{i.total} > {config.po_required_over}; no PO number', clauses['POL-1'])
            for i in invoices if i.total > config.po_required_over and not (i.po_number or '').strip()]


def split_checks(invoices: list[Invoice], clauses: dict[str, Citation], config: RuleConfig) -> list[Finding]:
    """Use disjoint earliest-anchored windows; never chain beyond seven days.

    Collapse exact duplicate records first to avoid counting the same purchase twice.
    A qualifying window is consumed once, producing one finding for the whole group.
    """
    unique = {}
    for invoice in sorted(invoices, key=lambda i: i.invoice_number):
        unique.setdefault(duplicate_key(invoice), invoice)
    vendors = defaultdict(list)
    for invoice in unique.values():
        if invoice.total < config.split_limit:
            vendors[invoice.vendor_name].append(invoice)
    results = []
    for vendor in sorted(vendors):
        ordered = sorted(vendors[vendor], key=lambda i: (i.invoice_date, i.invoice_number))
        start = 0
        while start < len(ordered):
            end = start + 1
            while end < len(ordered) and (ordered[end].invoice_date - ordered[start].invoice_date).days <= config.split_window_days:
                end += 1
            group = ordered[start:end]
            total = sum((i.total for i in group), Decimal(0))
            if len(group) > 1 and total > config.split_limit:
                results.append(finding('split_purchase', group, total,
                                       f'Combined total {total} > {config.split_limit} within {config.split_window_days} days',
                                       clauses['POL-3']))
                start = end
            else:
                start += 1
    return results


def unusual_checks(invoices: list[Invoice], history: list[dict], config: RuleConfig) -> list[Finding]:
    monthly = defaultdict(lambda: Decimal(0))
    for row in history:
        monthly[row['vendor_name'], row['month']] += Decimal(row['invoice_total'])
    highest = defaultdict(lambda: Decimal(0))
    for (vendor, _month), total in monthly.items():
        highest[vendor] = max(highest[vendor], total)
    results = []
    for invoice in invoices:
        if invoice.vendor_name not in highest:
            continue
        threshold = highest[invoice.vendor_name] * config.unusual_multiplier
        if invoice.total > threshold:
            results.append(finding('unusual_amount', [invoice], invoice.total - threshold,
                                   f'{invoice.total} - ({highest[invoice.vendor_name]} × {config.unusual_multiplier}) = {invoice.total - threshold}',
                                   confidence=0.7))
    return results


def run_rules(invoices: list[Invoice], contracts: list[dict], history: list[dict],
              policy_clauses: dict[str, Citation], config: RuleConfig | None = None) -> RuleResult:
    """Audit USD inputs. Other currencies need pipeline review, not USD comparisons."""
    if len({i.invoice_number for i in invoices}) != len(invoices):
        raise ValueError('Invoice numbers must be unique')
    referrals = [ReviewReferral(i.invoice_number, f'Unsupported currency {i.currency}; requires human review')
                 for i in sorted(invoices, key=lambda i: i.invoice_number) if i.currency != 'USD']
    invoices = [i for i in invoices if i.currency == 'USD']
    config = config or RuleConfig()
    results = (arithmetic_checks(invoices) + duplicate_checks(invoices)
               + contract_checks(invoices, contracts) + po_checks(invoices, policy_clauses, config)
               + split_checks(invoices, policy_clauses, config) + unusual_checks(invoices, history, config))
    return RuleResult(sorted(results, key=lambda f: (f.type, f.invoice_numbers, f.calculation)), referrals)
