"""Check generated inputs and independently recompute the planted answers."""
from collections import Counter
from datetime import date
from decimal import Decimal
import importlib.util
import json
import re
from pathlib import Path

import pytest

from ledgerguard.models import Invoice
from ledgerguard.synthetic import generate


def read(path):
    return json.loads(path.read_text())


@pytest.fixture(params=[7, 8, 9])
def dataset(generated, request):
    return generated(request.param)


def files(out):
    return {str(p.relative_to(out)): p.read_bytes() for p in out.rglob('*') if p.is_file()}


def test_deterministic_and_seed_changes_output(tmp_path, generated):
    # One deliberate regeneration proves independent runs match byte for byte.
    a, b, c = generated(7), tmp_path / 'repeat', generated(8)
    generate(7, b)
    assert files(a) == files(b)
    assert files(a) != files(c)


def test_every_invoice_validates_and_split_is_separate(dataset):
    splits = read(dataset / 'splits.json')
    paths = list((dataset / 'invoices').glob('*.json'))
    assert len(paths) == 57
    assert len(splits) == len(paths)
    for path in paths:
        invoice = Invoice.model_validate(read(path))
        assert splits[invoice.invoice_number] in {'dev', 'test'}
    assert Decimal('0.65') < Decimal(Counter(splits.values())['dev']) / len(paths) < Decimal('0.75')


def test_answer_references_and_coverage(dataset):
    splits = read(dataset / 'splits.json')
    answers = read(dataset / 'answer_key/expected_findings.json')
    counts = Counter((a['type'], a['split']) for a in answers)
    types = {'duplicate', 'contract_overcharge', 'arithmetic_error',
             'policy_violation', 'split_purchase', 'unusual_amount'}
    assert {a['type'] for a in answers} == types
    for kind in types:
        for split in ['dev', 'test']:
            assert counts[kind, split] >= 2
    for answer in answers:
        assert isinstance(answer['amount_at_issue'], str)
        assert Decimal(answer['amount_at_issue']) > 0
        for number in answer['invoice_numbers']:
            assert (dataset / 'invoices' / f'{number}.json').is_file()
            assert splits[number] == answer['split']


def test_clean_invoices_are_clean(dataset):
    contracts = {(c['vendor_name'], c['item']): c for c in read(dataset / 'contracts.json')}
    flagged = set()
    answers = read(dataset / 'answer_key/expected_findings.json')
    for answer in answers:
        # The original duplicate anchor remains a clean original invoice.
        flagged.update(answer['invoice_numbers'][1:] if answer['type'] == 'duplicate'
                       else answer['invoice_numbers'])
    clean = []
    for path in (dataset / 'invoices').glob('*.json'):
        invoice = Invoice.model_validate(read(path))
        if invoice.invoice_number in flagged:
            continue
        clean.append(invoice)
        assert invoice.tax == 0
        assert invoice.subtotal + invoice.tax == invoice.total
        assert sum(item.amount for item in invoice.line_items) == invoice.subtotal
        assert invoice.total <= 1000
        for item in invoice.line_items:
            assert item.unit_price == Decimal(contracts[invoice.vendor_name, item.description]['unit_price'])
            assert item.amount == item.unit_price * item.quantity
            assert item.description != 'Gift cards'
    assert len(clean) == 25
    # Even adjacent clean invoices cannot accidentally exceed the split threshold.
    for current in clean:
        nearby = [i for i in clean if i.vendor_name == current.vendor_name
                  and 0 <= (i.invoice_date - current.invoice_date).days <= 7]
        assert sum(i.total for i in nearby) < 5000


def test_exact_planted_amounts_and_clauses(dataset):
    records = {p.stem: Invoice.model_validate(read(p)) for p in (dataset / 'invoices').glob('*.json')}
    contracts = {c['vendor_name']: c for c in read(dataset / 'contracts.json')}
    history = read(dataset / 'history.json')
    for answer in read(dataset / 'answer_key/expected_findings.json'):
        invoices = [records[n] for n in answer['invoice_numbers']]
        invoice = invoices[-1]
        kind = answer['type']
        clause = None
        if kind == 'duplicate':
            original, duplicate = invoices
            assert original.model_dump(exclude={'invoice_number'}) == duplicate.model_dump(exclude={'invoice_number'})
            amount = duplicate.total
        elif kind == 'contract_overcharge':
            contract = contracts[invoice.vendor_name]
            amount = sum((item.unit_price - Decimal(contract['unit_price'])) * item.quantity
                         for item in invoice.line_items)
            clause = contract['clause_id']
            assert date.fromisoformat(contract['effective_from']) <= invoice.invoice_date <= date.fromisoformat(contract['effective_to'])
        elif kind == 'arithmetic_error':
            amount = abs(invoice.subtotal + invoice.tax - invoice.total)
        elif kind == 'policy_violation':
            if answer['clause_id'] == 'POL-1':
                assert invoice.total > 1000 and invoice.po_number is None
                amount, clause = invoice.total, 'POL-1'
            else:
                assert invoice.total < 1000 and invoice.po_number
                amount, clause = sum(i.amount for i in invoice.line_items), 'POL-4'
        elif kind == 'split_purchase':
            assert len({i.vendor_name for i in invoices}) == 1
            assert (max(i.invoice_date for i in invoices) - min(i.invoice_date for i in invoices)).days <= 7
            assert all(i.total < 5000 and i.po_number for i in invoices)
            amount, clause = sum(i.total for i in invoices), 'POL-3'
            assert amount > 5000
        else:
            usual = max(Decimal(h['invoice_total']) for h in history if h['vendor_name'] == invoice.vendor_name) * 3
            amount = invoice.total - usual
        assert amount == Decimal(answer['amount_at_issue'])
        assert clause == answer['clause_id']


def test_supporting_data_and_no_answers_in_inputs(dataset):
    assert len(read(dataset / 'vendors.json')) == 5
    assert len(read(dataset / 'contracts.json')) == 8
    history = read(dataset / 'history.json')
    assert set(Counter(h['vendor_id'] for h in history).values()) == {12}
    for path in (dataset / 'invoices').glob('*.json'):
        assert 'amount_at_issue' not in path.read_text()
        assert 'split' not in read(path)


def test_every_output_passes_financial_guard(dataset):
    spec = importlib.util.spec_from_file_location(
        'guard', Path(__file__).resolve().parents[1] / '.claude/hooks/checks.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for path in dataset.rglob('*'):
        if path.is_file():
            if path.suffix == '.pdf':
                # Scan visible literal strings, not random PDF-internal IDs.
                strings = re.findall(rb'\(((?:\\.|[^\\)])*)\)\s*Tj', path.read_bytes())
                text = '\n'.join(re.sub(rb'\\([\\()])', rb'\1', s).decode('latin-1') for s in strings)
                assert strings
            else:
                text = path.read_text()
            assert not guard.suspected_financial_data(text), str(path.relative_to(dataset))


def test_nonempty_output_refused_without_changes(dataset):
    before = files(dataset)
    with pytest.raises(ValueError, match='empty'):
        generate(8, dataset)
    assert files(dataset) == before


def test_no_unplanned_near_duplicates(dataset):
    from itertools import combinations

    allowed = {frozenset(a['invoice_numbers'])
               for a in read(dataset / 'answer_key/expected_findings.json')
               if a['type'] == 'duplicate'}
    records = [Invoice.model_validate(read(p)) for p in (dataset / 'invoices').glob('*.json')]
    actual = {frozenset([a.invoice_number, b.invoice_number])
              for a, b in combinations(records, 2)
              if (a.vendor_name, a.total) == (b.vendor_name, b.total)}
    assert actual == allowed


def test_pol4_semantic_examples_and_fully_clean_count(dataset):
    answers = read(dataset / 'answer_key/expected_findings.json')
    pol4 = [a for a in answers if a['clause_id'] == 'POL-4']
    assert Counter(a['split'] for a in pol4) == {'dev': 2, 'test': 2}
    descriptions = set()
    for answer in pol4:
        invoice = Invoice.model_validate(read(dataset / 'invoices' / (answer['invoice_numbers'][0] + '.json')))
        descriptions.update(item.description for item in invoice.line_items)
    assert len(descriptions) == 4
    assert all('gift card' not in text.lower() for text in descriptions)
    involved = {n for a in answers for n in a['invoice_numbers']}
    assert len(list((dataset / 'invoices').glob('*.json'))) - len(involved) == 21


def test_policy_is_general_with_semantic_positive_and_negative_cases(dataset):
    policy = (dataset / 'policy.md').read_text()
    assert 'POL-4: Gift cards and other cash equivalents are prohibited.' in policy
    answers = read(dataset / 'answer_key/expected_findings.json')
    involved = {n for answer in answers for n in answer['invoice_numbers']}
    splits = read(dataset / 'splits.json')
    harmless = {'Greeting cards', 'Business cards (500)', 'Gift wrap paper'}
    found = {}
    for path in (dataset / 'invoices').glob('*.json'):
        invoice = Invoice.model_validate(read(path))
        for item in invoice.line_items:
            if item.description in harmless:
                assert invoice.invoice_number not in involved
                found[item.description] = splits[invoice.invoice_number]
    assert set(found) == harmless
    assert set(found.values()) == {'dev', 'test'}
    positives = {item.description
                 for answer in answers if answer['clause_id'] == 'POL-4'
                 for number in answer['invoice_numbers']
                 for item in Invoice.model_validate(read(dataset / 'invoices' / f'{number}.json')).line_items}
    semantic = {'Reloadable spending card for staff', 'Store credit - staff rewards'}
    assert semantic <= positives
    assert all(not any(word in description.lower() for word in ['gift', 'cash', 'equivalent'])
               for description in semantic)
