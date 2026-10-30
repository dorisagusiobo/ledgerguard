"""Reproducible synthetic JSON fixtures; answer keys stay outside invoice inputs."""
import argparse
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import random

from ledgerguard.models import Invoice
from ledgerguard.pdf import line_item_source_text, render_invoice

CENT = Decimal('0.01')
POLICY = '''# Synthetic spending policy v1

POL-1: Invoices over $1,000 require a purchase order number.
POL-2: Purchases over $5,000 require manager approval.
POL-3: Do not split a purchase across invoices within 7 days to avoid POL-2.
POL-4: Gift cards and other cash equivalents are prohibited.

For this benchmark, split purchases are same-vendor invoices within 7 days,
each below $5,000 and combined above $5,000. An unusual amount exceeds three
times the maximum monthly historical invoice total for that vendor; the amount
at issue is the excess above that upper bound. An anomaly alone is not fraud.
'''


def money(value):
    return str(Decimal(value).quantize(CENT))


def generate(seed: int, out: Path) -> None:
    """Write 21 fully clean invoices, four duplicate originals, and planted errors.

    Include two examples per error type and two POL-4 cases in each split.

    Existing nonempty destinations are refused to avoid stale files or data loss.
    Split groups and duplicate pairs stay together. No global RNG state is used.
    """
    out = Path(out)
    if out.exists() and any(p.name != '.gitkeep' for p in out.iterdir()):
        raise ValueError('Output directory must be empty (except .gitkeep); use a new directory')
    rng = random.Random(seed)
    vendors = [{'id': f'VEN-{i + 1:03d}', 'name': f'Synthetic {name} Co.'}
               for i, name in enumerate(['Office Supply', 'Workshop', 'Packaging', 'Printing', 'Training'])]
    contracts = [{'vendor_id': v['id'], 'vendor_name': v['name'],
                  'item': f'Test item {i + 1}', 'unit_price': money(10 + i),
                  'clause_id': f"CON-{v['id']}-1", 'version': 'v1',
                  'effective_from': '2026-01-01', 'effective_to': '2026-12-31'}
                 for i, v in enumerate(vendors)]
    history = [{'vendor_id': v['id'], 'vendor_name': v['name'],
                'month': f'2025-{month:02d}', 'invoice_total': money(400 if i == 4 else 2000)}
               for i, v in enumerate(vendors) for month in range(1, 13)]
    invoices, splits, answers = {}, {}, []
    used_totals = set()

    def invoice(vendor, quantities, day, split, *, price=None, po=True, total_offset=Decimal(0), description=None):
        number = f'INV-2026-{len(invoices) + 1:04d}'
        rate = Decimal(contracts[vendor]['unit_price']) if price is None else Decimal(price)
        # Draw without replacement from the allowed quantity range. Compare final
        # totals, including planted arithmetic differences, before reserving a key.
        candidates = list(quantities)
        rng.shuffle(candidates)
        for quantity in candidates:
            amount = money(rate * Decimal(quantity))
            total = money(Decimal(amount) + total_offset)
            key = (vendors[vendor]['name'], total)
            if key not in used_totals:
                used_totals.add(key)
                break
        else:
            raise ValueError('No unique vendor/total remains in the quantity range')
        item = description or contracts[vendor]['item']
        record = {'invoice_number': number, 'vendor_name': vendors[vendor]['name'],
                  'po_number': f'PO-2026-{len(invoices) + 1:04d}' if po else None,
                  'invoice_date': day.isoformat(), 'due_date': (day + timedelta(days=30)).isoformat(),
                  'currency': 'USD', 'subtotal': amount, 'tax': '0.00', 'total': total,
                  'line_items': [{'description': item, 'quantity': str(quantity),
                                  'unit_price': str(rate), 'amount': amount,
                                  'source': {'page': 1, 'text': line_item_source_text(item, quantity, rate, Decimal(amount))}}]}
        invoices[number], splits[number] = record, split
        return number

    def finding(kind, numbers, amount, clause, split):
        answers.append({'type': kind, 'invoice_numbers': numbers,
                        'amount_at_issue': money(amount), 'clause_id': clause, 'split': split})

    # Clean duplicate anchors: keep both members of each pair in the same split.
    anchors = []
    lookalikes = {4: 'Greeting cards', 5: 'Business cards (500)', 6: 'Gift wrap paper'}
    for i in range(25):
        split = 'test' if i in (2, 3, 4) else 'dev'
        description = lookalikes.get(i)
        number = invoice(i % 5, range(1, 21), date(2026, 1, 1) + timedelta(days=i), split,
                         description=description)
        if description:
            # Explicit contract coverage keeps look-alikes clean on price and item.
            contract = dict(contracts[i % 5])
            contract['item'] = description
            contract['clause_id'] = f"CON-{vendors[i % 5]['id']}-2"
            contracts.append(contract)
        anchors.append(number)

    for split_index, split in enumerate(['dev', 'test']):
        for example in range(2):
            block = split_index * 2 + example
            day = date(2026, 3, 1) + timedelta(days=block * 50)
            anchor = anchors[block]
            duplicate = deepcopy(invoices[anchor])
            number = anchor + '-A'
            duplicate['invoice_number'] = number
            invoices[number], splits[number] = duplicate, split
            finding('duplicate', [anchor, number], duplicate['total'], None, split)

            number = invoice(2, range(5, 31), day, split, price='14.00')
            qty = Decimal(invoices[number]['line_items'][0]['quantity'])
            finding('contract_overcharge', [number], Decimal('2.00') * qty,
                    contracts[2]['clause_id'], split)

            number = invoice(3, range(5, 31), day + timedelta(days=9), split,
                             total_offset=Decimal('7.00'))
            finding('arithmetic_error', [number], '7.00', None, split)

            number = invoice(1, range(91, 141), day + timedelta(days=18), split, po=False)
            finding('policy_violation', [number], invoices[number]['total'], 'POL-1', split)

            first = invoice(0, range(251, 301), day + timedelta(days=27), split)
            second = invoice(0, range(251, 301), day + timedelta(days=29), split)
            combined = Decimal(invoices[first]['total']) + Decimal(invoices[second]['total'])
            finding('split_purchase', [first, second], combined, 'POL-3', split)

            number = invoice(4, range(90, 141), day + timedelta(days=36), split)
            upper = max(Decimal(h['invoice_total']) for h in history
                        if h['vendor_id'] == vendors[4]['id']) * 3
            excess = Decimal(invoices[number]['total']) - upper
            finding('unusual_amount', [number], excess, None, split)

            wording = ['Reloadable spending card for staff', 'Store credit - staff rewards',
                       'Prepaid Visa card', 'Holiday gift certificates'][block]
            number = invoice(3, range(1, 21), day + timedelta(days=43), split,
                             description=wording)
            finding('policy_violation', [number], invoices[number]['line_items'][0]['amount'],
                    'POL-4', split)

    # Validate everything before creating any output files.
    validated = {number: Invoice.model_validate(record) for number, record in invoices.items()}
    pdfs = {number: render_invoice(record) for number, record in validated.items()}
    out.mkdir(parents=True, exist_ok=True)

    def write(name, data):
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')

    write('metadata.json', {'seed': seed})
    write('vendors.json', vendors)
    write('contracts.json', contracts)
    write('history.json', history)
    write('splits.json', splits)
    (out / 'policy.md').write_text(POLICY)
    for number, record in validated.items():
        write(f'invoices/{number}.json', record.model_dump(mode='json'))
    (out / 'invoices_pdf').mkdir()
    for number, content in pdfs.items():
        (out / 'invoices_pdf' / f'{number}.pdf').write_bytes(content)
    write('answer_key/expected_findings.json', answers)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--out', type=Path, default=Path('data/synthetic'))
    args = parser.parse_args()
    try:
        generate(args.seed, args.out)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == '__main__':
    main()
