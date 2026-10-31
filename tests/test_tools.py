"""Read-only tool behavior and strict, bounded argument validation."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError

from ledgerguard.models import Invoice
from ledgerguard.rules import load_inputs
from ledgerguard.tools import DuplicateSearchArgs, ReadOnlyTools, RelatedInvoicesArgs, VendorHistoryArgs


def invoice(number='INV-2026-0001', total='100.00', day=0, vendor='PRIVATE-VENDOR'):
    return Invoice(invoice_number=number, vendor_name=vendor, po_number='PO-2026-0001',
                   invoice_date=date(2026, 1, 1) + timedelta(days=day), due_date='2026-02-01',
                   currency='USD', subtotal=total, tax='0.00', total=total,
                   line_items=[{'description': 'PRIVATE-LINE-TEXT', 'quantity': '1', 'unit_price': total,
                                'amount': total, 'source': {'page': 1, 'text': 'PRIVATE-SOURCE'}}])


def test_generated_split_and_duplicate_groups_without_answers(generated):
    inputs = load_inputs(generated(7))
    tools = ReadOnlyTools(inputs.invoices, inputs.history)
    # Find matching pairs independently, never using evaluation answers.
    duplicate = next((a, b) for a in inputs.invoices for b in inputs.invoices
                     if b.invoice_number == a.invoice_number + '-A')
    rows = tools.duplicate_search(duplicate[0].invoice_number)['rows']
    assert [r['invoice_number'] for r in rows] == [duplicate[1].invoice_number]
    split = next((a, b) for a in inputs.invoices for b in inputs.invoices
                 if a.invoice_number < b.invoice_number and a.vendor_name == b.vendor_name
                 and 0 <= (b.invoice_date - a.invoice_date).days <= 7
                 and a.total < 5000 and b.total < 5000 and a.total + b.total > 5000)
    result = tools.related_invoices(split[0].vendor_name, split[0].invoice_date, split[1].invoice_date)
    assert {i.invoice_number for i in split} <= {r['invoice_number'] for r in result['rows']}


def test_monthly_totals_unknowns_and_no_line_text():
    tools = ReadOnlyTools([invoice()], [
        {'vendor_name': 'PRIVATE-VENDOR', 'month': '2025-02', 'invoice_total': '10.00'},
        {'vendor_name': 'PRIVATE-VENDOR', 'month': '2025-01', 'invoice_total': '20.00'},
        {'vendor_name': 'PRIVATE-VENDOR', 'month': '2025-02', 'invoice_total': '5.00'},
    ])
    assert tools.vendor_history('PRIVATE-VENDOR') == {'rows': [
        {'month': '2025-01', 'total': '20.00'}, {'month': '2025-02', 'total': '15.00'}], 'truncated': False}
    assert tools.vendor_history('Unknown') == {'rows': [], 'truncated': False}
    assert tools.related_invoices('Unknown', date(2026, 1, 1), date(2026, 1, 31))['rows'] == []
    assert tools.duplicate_search('INV-UNKNOWN')['rows'] == []
    rows = tools.related_invoices('PRIVATE-VENDOR', date(2026, 1, 1), date(2026, 1, 1))['rows']
    assert set(rows[0]) == {'invoice_number', 'invoice_date', 'total', 'po_number'}
    assert 'PRIVATE-LINE-TEXT' not in str(rows) and 'PRIVATE-SOURCE' not in str(rows)


def test_all_tools_cap_sorted_results_at_twenty():
    invoices = [invoice(f'INV-2026-{i:04d}', day=i) for i in range(25)]
    history = [{'vendor_name': 'PRIVATE-VENDOR', 'month': f'{2020 + i // 12}-{i % 12 + 1:02d}',
                'invoice_total': '1.00'} for i in range(25)]
    tools = ReadOnlyTools(invoices[::-1], history[::-1])
    for result in [tools.vendor_history('PRIVATE-VENDOR'), tools.duplicate_search(invoices[0].invoice_number),
                   tools.related_invoices('PRIVATE-VENDOR', date(2026, 1, 1), date(2026, 1, 31))]:
        assert len(result['rows']) == 20 and result['truncated'] is True
    assert tools.related_invoices('PRIVATE-VENDOR', date(2026, 1, 1), date(2026, 1, 20))['truncated'] is False


def test_same_number_resubmission_is_returned():
    first, other = invoice(), invoice(vendor='Other vendor', total='200.00')
    result = ReadOnlyTools([first, other], []).duplicate_search(first.invoice_number)
    assert len(result['rows']) == 1 and Decimal(result['rows'][0]['total']) == 200


@pytest.mark.parametrize('model,args', [
    (VendorHistoryArgs, {'vendor_name': 'PRIVATE-VENDOR', 'extra': True}),
    (DuplicateSearchArgs, {'invoice_number': 'INV-2026-0001', 'extra': True}),
    (RelatedInvoicesArgs, {'vendor_name': 'PRIVATE-VENDOR', 'start': '2026-01-01', 'end': '2026-01-02', 'extra': True}),
    (RelatedInvoicesArgs, {'vendor_name': 'PRIVATE-VENDOR', 'start': '2026-01-01', 'end': '2026-02-02'}),
    (RelatedInvoicesArgs, {'vendor_name': 'PRIVATE-VENDOR', 'start': '2026-01-02', 'end': '2026-01-01'}),
    (RelatedInvoicesArgs, {'vendor_name': 'PRIVATE-VENDOR', 'start': 12345, 'end': '2026-01-02'}),
    (RelatedInvoicesArgs, {'vendor_name': 'PRIVATE-VENDOR', 'start': 'not-a-date', 'end': '2026-01-02'}),
])
def test_invalid_arguments(model, args):
    with pytest.raises(ValidationError):
        model.model_validate(args)


def test_window_boundary_and_dates():
    args = RelatedInvoicesArgs(vendor_name='PRIVATE-VENDOR', start='2026-01-01', end='2026-02-01')
    assert (args.end - args.start).days == 31
    assert RelatedInvoicesArgs(vendor_name='PRIVATE-VENDOR', start=args.start, end=args.start).start == args.start


def test_read_only_inputs_and_results(generated):
    inputs = load_inputs(generated(7))
    before = deepcopy(inputs)
    tools = ReadOnlyTools(inputs.invoices, inputs.history)
    for record in inputs.invoices:
        tools.vendor_history(record.vendor_name)
        tools.related_invoices(record.vendor_name, record.invoice_date, record.invoice_date)
        tools.duplicate_search(record.invoice_number)
    assert inputs == before
    result = tools.related_invoices(inputs.invoices[0].vendor_name, date(2026, 1, 1), date(2026, 1, 31))
    original = deepcopy(result)
    result['rows'].clear()
    assert tools.related_invoices(inputs.invoices[0].vendor_name, date(2026, 1, 1), date(2026, 1, 31)) == original


def test_no_answer_access_or_tool_file_access(generated, tmp_path, monkeypatch):
    source = tmp_path / 'inputs'
    shutil.copytree(generated(7), source)
    shutil.rmtree(source / 'answer_key')
    inputs = load_inputs(source)
    module_path = Path(__file__).resolve().parents[1] / 'src/ledgerguard/tools.py'
    assert 'answer_key' not in module_path.read_text()
    monkeypatch.setattr(Path, 'open', lambda *a, **k: pytest.fail('Tools must not open files'))
    tools = ReadOnlyTools(inputs.invoices, inputs.history)
    assert tools.vendor_history(inputs.invoices[0].vendor_name)['rows']
    assert tools.duplicate_search('Unknown')['rows'] == []
