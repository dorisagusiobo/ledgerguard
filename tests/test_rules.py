"""Hand-built boundaries and an answer-key-blind rules baseline."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import shutil

import pytest

from ledgerguard.models import Citation, Invoice
from ledgerguard.rules import (
    RuleConfig, arithmetic_checks, contract_checks, duplicate_checks, load_inputs,
    po_checks, run_rules, split_checks, unusual_checks,
)
from ledgerguard.synthetic import generate


@pytest.fixture
def config():
    return RuleConfig()


@pytest.fixture
def clauses():
    return {key: Citation(document='policy.md', version='v1', clause_id=key, quote=text)
            for key, text in [('POL-1', 'POL-1: A purchase order is required above the limit.'),
                              ('POL-3', 'POL-3: Do not split purchases to avoid approval.') ]}


def inv(number='INV-2026-0001', total='100.00', *, day=0, po='PO-2026-0001', vendor='Synthetic Vendor'):
    return Invoice(invoice_number=number, vendor_name=vendor, po_number=po,
                   invoice_date=date(2026, 1, 1) + timedelta(days=day),
                   due_date=date(2026, 2, 1), currency='USD',
                   subtotal=total, tax='0.00', total=total,
                   line_items=[{'description': 'Test item', 'quantity': '1',
                                'unit_price': total, 'amount': total,
                                'source': {'page': 1, 'text': f'Test item ${total}'}}])


def contract(**overrides):
    return {'vendor_name': 'Synthetic Vendor', 'item': 'Test item',
            'unit_price': '10.00', 'version': 'v1', 'clause_id': 'CON-VEN-001-1',
            'effective_from': '2026-01-01', 'effective_to': '2026-01-31', **overrides}


def test_arithmetic_clean():
    assert arithmetic_checks([inv()]) == []


@pytest.mark.parametrize('problem', ['line', 'subtotal', 'total'])
def test_each_arithmetic_equation(problem):
    invoice = inv()
    if problem == 'line':
        invoice.line_items[0].unit_price = Decimal('93.00')
    elif problem == 'subtotal':
        invoice.subtotal = Decimal('107.00')
        invoice.total = Decimal('107.00')
    else:
        invoice.total = Decimal('107.00')
    results = arithmetic_checks([invoice])
    assert len(results) == 1
    assert results[0].amount_at_issue == Decimal('7.00')
    assert results[0].confidence == 1.0
    assert results[0].no_clause_reason == 'no applicable clause identified'
    assert results[0].evidence == [invoice.line_items[0].source]


def test_fractional_unit_price_rounds_to_cents():
    invoice = inv(total='0.13')
    invoice.line_items[0].unit_price = Decimal('0.125')
    assert arithmetic_checks([invoice]) == []


def test_duplicate_once_and_stable():
    first, second = inv(), inv('INV-2026-0001-A')
    results = duplicate_checks([second, first])
    assert len(results) == 1
    assert results[0].invoice_numbers == [first.invoice_number, second.invoice_number]
    assert results[0].amount_at_issue == second.total
    assert results == duplicate_checks([first, second])


@pytest.mark.parametrize('change', ['vendor', 'date', 'po', 'item'])
def test_not_exact_duplicate(change):
    first, second = inv(), inv('INV-2026-0001-A')
    if change == 'vendor':
        second.vendor_name = 'Synthetic Other Vendor'
    elif change == 'date':
        second.invoice_date += timedelta(days=1)
    elif change == 'po':
        second.po_number = 'PO-2026-0002'
    else:
        second.line_items[0].description = 'Other item'
    assert duplicate_checks([first, second]) == []


@pytest.mark.parametrize('price,expected', [('9.99', None), ('10.00', None), ('10.01', '0.01')])
def test_price_boundary(price, expected):
    invoice = inv(total=price)
    results = contract_checks([invoice], [contract()])
    assert len(results) == (0 if expected is None else 1)
    if results:
        assert results[0].amount_at_issue == Decimal(expected)
        assert results[0].citation.quote == 'Test item: $10.00 per unit'
        assert results[0].confidence == 1.0


@pytest.mark.parametrize('day,expected', [(-1, False), (0, True), (30, True), (31, False)])
def test_contract_effective_dates(day, expected):
    assert bool(contract_checks([inv(total='11.00', day=day)], [contract()])) == expected


def test_no_matching_contract_and_quantity_calculation():
    invoice = inv(total='24.00')
    invoice.line_items[0].quantity = Decimal('2')
    invoice.line_items[0].unit_price = Decimal('12.00')
    assert contract_checks([invoice], []) == []
    assert contract_checks([invoice], [contract(item='Unrelated')]) == []
    assert contract_checks([invoice], [contract(vendor_name='Other Vendor')]) == []
    assert contract_checks([invoice], [contract()])[0].amount_at_issue == Decimal('4.00')


@pytest.mark.parametrize('total,expected', [('999.99', False), ('1000.00', False), ('1000.01', True)])
def test_po_boundary(total, expected, clauses, config):
    invoice = inv(total=total, po=None)
    results = po_checks([invoice], clauses, config)
    assert bool(results) == expected
    if results:
        assert results[0].citation == clauses['POL-1']
        assert results[0].amount_at_issue == invoice.total
    invoice.po_number = 'PO-2026-0001'
    assert po_checks([invoice], clauses, config) == []


@pytest.mark.parametrize('day,expected', [(6, True), (7, True), (8, False)])
def test_split_window_boundary(day, expected, clauses, config):
    invoices = [inv(total='2600.00'), inv('INV-2026-0002', total='2700.00', day=day)]
    results = split_checks(invoices, clauses, config)
    assert bool(results) == expected
    if results:
        assert results[0].amount_at_issue == Decimal('5300.00')
        assert results[0].citation == clauses['POL-3']


@pytest.mark.parametrize('second,expected', [('2499.99', False), ('2500.00', False), ('2500.01', True)])
def test_combined_split_boundary(second, expected, clauses, config):
    assert bool(split_checks([inv(total='2500.00'), inv('INV-2026-0002', total=second, day=1)], clauses, config)) == expected


@pytest.mark.parametrize('total,expected', [('4999.99', True), ('5000.00', False), ('5000.01', False)])
def test_each_split_invoice_must_be_under_limit(total, expected, clauses, config):
    assert bool(split_checks([inv(total=total), inv('INV-2026-0002', total='100.00', day=1)], clauses, config)) == expected


def test_three_invoice_split_once_and_no_chained_window(clauses, config):
    invoices = [inv(f'INV-2026-000{i + 1}', total='2000.00', day=day) for i, day in enumerate([0, 3, 7])]
    results = split_checks(invoices[::-1], clauses, config)
    assert len(results) == 1
    assert results[0].invoice_numbers == sorted(i.invoice_number for i in invoices)
    assert results[0].amount_at_issue == Decimal('6000.00')
    assert results == split_checks(invoices, clauses, config)
    invoices[2].invoice_date = date(2026, 1, 9)
    assert split_checks(invoices, clauses, config) == []


def test_duplicate_does_not_create_split(clauses, config):
    assert split_checks([inv(total='3000.00'), inv('INV-2026-0001-A', total='3000.00')], clauses, config) == []
    assert split_checks([inv(total='3000.00'), inv('INV-2026-0002', total='3000.00', vendor='Other')], clauses, config) == []


@pytest.mark.parametrize('total,expected', [('299.99', False), ('300.00', False), ('300.01', True)])
def test_unusual_boundary(total, expected, config):
    invoice = inv(total=total)
    history = [{'vendor_name': invoice.vendor_name, 'month': '2025-01', 'invoice_total': '100.00'}]
    results = unusual_checks([invoice], history, config)
    assert bool(results) == expected
    if results:
        assert results[0].amount_at_issue == Decimal('0.01')
        assert results[0].confidence == 0.7
    assert unusual_checks([invoice], [], config) == []


def test_history_sums_months_before_selecting_highest(config):
    history = [{'vendor_name': 'Synthetic Vendor', 'month': month, 'invoice_total': value}
               for month, value in [('2025-01', '60.00'), ('2025-01', '40.00'), ('2025-02', '80.00')]]
    assert unusual_checks([inv(total='300.00')], history, config) == []


def test_configuration_changes_thresholds(clauses):
    config = RuleConfig(po_required_over='2000', split_limit='7000', split_window_days=3, unusual_multiplier='4')
    assert po_checks([inv(total='1500.00', po=None)], clauses, config) == []
    assert split_checks([inv(total='3500.00'), inv('INV-2026-0002', total='3600.00', day=4)], clauses, config) == []
    history = [{'vendor_name': 'Synthetic Vendor', 'month': '2025-01', 'invoice_total': '100'}]
    assert unusual_checks([inv(total='400.00')], history, config) == []


@pytest.mark.parametrize('seed', [7, 8, 9])
def test_full_generated_baseline_and_loader_is_answer_blind(tmp_path, monkeypatch, seed):
    generate(seed, tmp_path)
    expected = json.loads((tmp_path / 'answer_key/expected_findings.json').read_text())
    shutil.rmtree(tmp_path / 'answer_key')
    original_read = Path.read_text

    def guarded_read(path, *args, **kwargs):
        assert 'answer_key' not in path.parts
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', guarded_read)
    inputs = load_inputs(tmp_path)
    before = deepcopy(inputs)
    outcome = run_rules(**vars(inputs))
    assert outcome.needs_review == []
    results = outcome.findings
    assert inputs == before
    actual = [(f.type, tuple(f.invoice_numbers), f.amount_at_issue,
               f.citation.clause_id if f.citation else None) for f in results]
    wanted = [(f['type'], tuple(sorted(f['invoice_numbers'])), Decimal(f['amount_at_issue']), f['clause_id'])
              for f in expected if f['clause_id'] != 'POL-4']
    assert sorted(actual) == sorted(wanted)
    assert len(results) == 24 and len(expected) == 28
    assert outcome == run_rules(inputs.invoices[::-1], inputs.contracts[::-1], inputs.history[::-1], inputs.policy_clauses)
    policy = (tmp_path / 'policy.md').read_text()
    for result in results:
        assert result.evidence and result.status == 'pending'
        if result.citation and result.citation.document == 'policy.md':
            assert result.citation.quote in policy.splitlines()


def test_non_usd_requires_review_and_duplicate_ids_rejected(clauses):
    invoice = inv()
    invoice.currency = 'EUR'
    usd = inv('INV-2026-0002', total='1100.00', po=None)
    result = run_rules([invoice, usd], [], [], clauses)
    assert len(result.needs_review) == 1
    assert result.needs_review[0].invoice_number == invoice.invoice_number
    assert 'EUR' in result.needs_review[0].reason
    assert result.findings == run_rules([usd], [], [], clauses).findings
    assert len(result.findings) == 1
    assert result.findings[0].invoice_numbers == [usd.invoice_number]
    assert run_rules([invoice], [], [], clauses).findings == []
    with pytest.raises(ValueError, match='unique'):
        run_rules([inv(), inv()], [], [], clauses)


def test_line_typo_is_one_finding_with_zero_money_at_stake():
    invoice = inv(total='130.00')
    invoice.line_items[0].quantity = Decimal('10')
    invoice.line_items[0].unit_price = Decimal('13.00')
    invoice.line_items[0].amount = Decimal('137.00')
    results = arithmetic_checks([invoice])
    assert len(results) == 1
    assert results[0].amount_at_issue == Decimal('0.00')
    assert 'line 1: quantity × unit price' in results[0].calculation
    assert 'line sum vs subtotal' in results[0].calculation
    assert 'subtotal + tax vs total' not in results[0].calculation


def test_arithmetic_amount_is_total_exposure_not_sum_of_mismatches():
    invoice = inv(total='150.00')
    invoice.line_items[0].quantity = Decimal('10')
    invoice.line_items[0].unit_price = Decimal('13.00')
    invoice.line_items[0].amount = Decimal('137.00')
    invoice.subtotal = Decimal('140.00')
    result, = arithmetic_checks([invoice])
    assert result.amount_at_issue == Decimal('20.00')
    for label in ['line 1:', 'line sum vs subtotal', 'subtotal + tax vs total']:
        assert label in result.calculation


@pytest.mark.parametrize('shared_clause', [False, True])
def test_overcharges_grouped_by_invoice_and_real_clause(shared_clause):
    invoice = inv(total='24.00')
    invoice.line_items[0].unit_price = Decimal('12.00')
    invoice.line_items[0].quantity = Decimal('2')
    second = invoice.line_items[0].model_copy(deep=True)
    second.description = 'Second item'
    second.quantity = Decimal('3')
    second.unit_price = Decimal('11.00')
    second.amount = Decimal('33.00')
    invoice.line_items.append(second)
    second_clause = 'CON-VEN-001-1' if shared_clause else 'CON-VEN-001-2'
    contracts = [contract(), contract(item='Second item', clause_id=second_clause, unit_price='9.00')]
    results = contract_checks([invoice], contracts)
    assert len(results) == (1 if shared_clause else 2)
    by_clause = {result.citation.clause_id: result for result in results}
    assert set(by_clause) == {record['clause_id'] for record in contracts}
    if shared_clause:
        assert by_clause['CON-VEN-001-1'].amount_at_issue == Decimal('10.00')
    else:
        assert by_clause['CON-VEN-001-1'].amount_at_issue == Decimal('4.00')
        assert by_clause['CON-VEN-001-2'].amount_at_issue == Decimal('6.00')
    for record in contracts:
        result = by_clause[record['clause_id']]
        assert result.invoice_numbers == [invoice.invoice_number]
        assert result.citation.version == record['version']
        assert f"{record['item']}: ${record['unit_price']} per unit" in result.citation.quote
    invoice.line_items.reverse()
    assert contract_checks([invoice], contracts[::-1]) == results
