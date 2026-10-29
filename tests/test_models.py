"""Synthetic contracts for extraction, rule checks, and review."""
from decimal import Decimal
import json

import pytest
from pydantic import ValidationError

from ledgerguard.models import Citation, Finding, Invoice, LineItem, SourceRef


@pytest.fixture
def invoice():
    return {
        'invoice_number': 'INV-2026-0001', 'vendor_name': 'Synthetic Supplies',
        'po_number': 'PO-2026-0001', 'invoice_date': '2026-10-01',
        'due_date': '2026-10-31', 'currency': 'USD',
        'subtotal': '20.00', 'tax': '2.00', 'total': '22.00',
        'line_items': [{'description': 'Test paper', 'quantity': '2',
                        'unit_price': '10.00', 'amount': '20.00',
                        'source': {'page': 1, 'text': 'Test paper: 2 x $10.00 = $20.00'}}],
    }


@pytest.fixture
def citation():
    return {'document': 'Synthetic contract', 'version': 'v1',
            'clause_id': 'RATE-001', 'quote': 'Test paper costs $10 per unit.'}


@pytest.fixture
def finding():
    return {'type': 'unusual_amount', 'invoice_numbers': ['INV-2026-0001'],
            'amount_at_issue': '22.00', 'calculation': 'Entire invoice total: $22.00',
            'evidence': [{'page': 1, 'text': 'Total $22.00'}],
            'no_clause_reason': 'no applicable clause identified', 'confidence': 0.8}


def test_valid_invoice_and_json_round_trip(invoice):
    parsed = Invoice.model_validate(invoice)
    assert parsed.total == Decimal('22.00')
    assert isinstance(parsed.line_items[0].unit_price, Decimal)
    assert parsed.invoice_date.isoformat() == '2026-10-01'
    assert Invoice.model_validate_json(parsed.model_dump_json()) == parsed
    invoice.pop('po_number')
    assert Invoice.model_validate(invoice).po_number is None


@pytest.mark.parametrize('field', ['subtotal', 'tax', 'total'])
@pytest.mark.parametrize('value', [0.1, '-0.01', 'NaN', 'Infinity'])
def test_invalid_invoice_money(invoice, field, value):
    invoice[field] = value
    with pytest.raises(ValidationError):
        Invoice.model_validate(invoice)
    with pytest.raises(ValidationError):
        Invoice.model_validate_json(json.dumps(invoice))


@pytest.mark.parametrize('field', ['unit_price', 'amount'])
@pytest.mark.parametrize('value', [0.1, '-0.01'])
def test_invalid_line_money(invoice, field, value):
    invoice['line_items'][0][field] = value
    with pytest.raises(ValidationError):
        Invoice.model_validate(invoice)


@pytest.mark.parametrize('value', [0.1, '-0.01'])
def test_invalid_finding_money(finding, value):
    finding['amount_at_issue'] = value
    with pytest.raises(ValidationError):
        Finding.model_validate(finding)


@pytest.mark.parametrize('model', [SourceRef, LineItem, Invoice, Citation, Finding])
def test_unknown_fields_forbidden(model, invoice, citation, finding):
    data = {SourceRef: invoice['line_items'][0]['source'],
            LineItem: invoice['line_items'][0], Invoice: invoice,
            Citation: citation, Finding: finding}[model]
    with pytest.raises(ValidationError, match='extra_forbidden'):
        model.model_validate({**data, 'invented_field': 'unexpected'})


def test_finding_with_reason(finding):
    parsed = Finding.model_validate(finding)
    assert parsed.status == 'pending'
    assert parsed.citation is None
    assert parsed.amount_at_issue == Decimal('22.00')


def test_finding_with_citation(finding, citation):
    finding.pop('no_clause_reason')
    finding['citation'] = citation
    assert Finding.model_validate(finding).citation.quote == citation['quote']


def test_both_citation_and_reason_rejected(finding, citation):
    finding['citation'] = citation
    with pytest.raises(ValidationError, match='exactly one'):
        Finding.model_validate(finding)


def test_neither_citation_nor_reason_rejected(finding):
    finding.pop('no_clause_reason')
    with pytest.raises(ValidationError, match='exactly one'):
        Finding.model_validate(finding)


def test_reason_must_be_exact(finding):
    finding['no_clause_reason'] = 'No clause found'
    with pytest.raises(ValidationError):
        Finding.model_validate(finding)


def test_wrong_arithmetic_still_parses(invoice):
    invoice['total'] = '999.00'
    invoice['line_items'][0]['amount'] = '17.00'
    parsed = Invoice.model_validate(invoice)
    assert parsed.total == Decimal('999.00')
    assert parsed.line_items[0].amount == Decimal('17.00')


@pytest.mark.parametrize('currency', ['USD', 'EUR', 'GBP'])
def test_three_letter_currencies_allowed(invoice, currency):
    invoice['currency'] = currency
    assert Invoice.model_validate(invoice).currency == currency


@pytest.mark.parametrize('currency', ['US', 'USDD', 'usd', '123'])
def test_invalid_currency_rejected(invoice, currency):
    invoice['currency'] = currency
    with pytest.raises(ValidationError):
        Invoice.model_validate(invoice)


def test_empty_line_items_rejected(invoice):
    invoice['line_items'] = []
    with pytest.raises(ValidationError):
        Invoice.model_validate(invoice)


@pytest.mark.parametrize('page', [0, -1, 1.5])
def test_source_page_rejected(page):
    with pytest.raises(ValidationError):
        SourceRef(page=page, text='Synthetic source text')


@pytest.mark.parametrize('field,value', [('confidence', -0.1), ('confidence', 1.1),
                                        ('type', 'fraud'), ('status', 'paid'),
                                        ('invoice_numbers', []), ('evidence', [])])
def test_invalid_finding_fields(finding, field, value):
    finding[field] = value
    with pytest.raises(ValidationError):
        Finding.model_validate(finding)


@pytest.mark.parametrize('model,fields', [
    (LineItem, ['amount', 'unit_price', 'quantity']),
    (Invoice, ['subtotal', 'tax', 'total']),
    (Finding, ['amount_at_issue']),
])
def test_decimal_json_schema_advertises_strings(model, fields):
    properties = model.model_json_schema()['properties']
    for field in fields:
        assert properties[field]['type'] == 'string'
        assert 'anyOf' not in properties[field]


@pytest.mark.parametrize('field', ['subtotal', 'tax', 'total'])
def test_invoice_amount_precision(invoice, field):
    invoice[field] = '20.001'
    with pytest.raises(ValidationError, match='decimal_max_places'):
        Invoice.model_validate(invoice)


def test_line_amount_precision(invoice):
    invoice['line_items'][0]['amount'] = '20.001'
    with pytest.raises(ValidationError, match='decimal_max_places'):
        Invoice.model_validate(invoice)


def test_finding_amount_precision(finding):
    finding['amount_at_issue'] = '20.001'
    with pytest.raises(ValidationError, match='decimal_max_places'):
        Finding.model_validate(finding)


def test_unit_price_allows_four_decimal_places(invoice):
    for value in ['0.125', '0.1256']:
        invoice['line_items'][0]['unit_price'] = value
        parsed = Invoice.model_validate(invoice)
        assert parsed.line_items[0].unit_price == Decimal(value)
    invoice['line_items'][0]['unit_price'] = '0.12567'
    with pytest.raises(ValidationError, match='decimal_max_places'):
        Invoice.model_validate(invoice)


@pytest.mark.parametrize('model,field', [
    (SourceRef, 'text'), (LineItem, 'description'), (Invoice, 'vendor_name'),
    (Citation, 'quote'), (Finding, 'calculation'),
])
def test_blank_text_rejected(model, field, invoice, citation, finding):
    data = {SourceRef: invoice['line_items'][0]['source'],
            LineItem: invoice['line_items'][0], Invoice: invoice,
            Citation: citation, Finding: finding}[model]
    with pytest.raises(ValidationError, match='string_too_short'):
        model.model_validate({**data, field: '   '})
