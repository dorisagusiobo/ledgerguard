"""Pure read-only views of loaded invoices and vendor history."""
from collections import defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal
import re

from pydantic import Field, field_validator, model_validator

from ledgerguard.models import Invoice, Schema

MAX_ROWS = 20


class VendorHistoryArgs(Schema):
    vendor_name: str = Field(min_length=1)


class RelatedInvoicesArgs(Schema):
    vendor_name: str = Field(min_length=1)
    start: date
    end: date

    @field_validator('start', 'end', mode='before')
    @classmethod
    def calendar_date(cls, value):
        if type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return date.fromisoformat(value)
        raise ValueError('Use a calendar date in YYYY-MM-DD format')

    @model_validator(mode='after')
    def bounded_window(self):
        if not 0 <= (self.end - self.start).days <= 31:
            raise ValueError('Window must be ordered and at most 31 days apart')
        return self


class DuplicateSearchArgs(Schema):
    invoice_number: str = Field(min_length=1)


TOOL_ARGUMENTS = {
    'vendor_history': VendorHistoryArgs,
    'related_invoices': RelatedInvoicesArgs,
    'duplicate_search': DuplicateSearchArgs,
}


def limited(rows):
    return {'rows': rows[:MAX_ROWS], 'truncated': len(rows) > MAX_ROWS}


def invoice_row(invoice):
    return {'invoice_number': invoice.invoice_number,
            'invoice_date': invoice.invoice_date.isoformat(),
            'total': str(invoice.total), 'po_number': invoice.po_number}


class ReadOnlyTools:
    def __init__(self, invoices: list[Invoice], history: list[dict]):
        # Own a snapshot so neither callers nor returned rows can mutate inputs.
        self._invoices = tuple(deepcopy(invoices))
        self._history = tuple(deepcopy(history))

    def vendor_history(self, vendor_name: str) -> dict:
        args = VendorHistoryArgs(vendor_name=vendor_name)
        totals = defaultdict(lambda: Decimal(0))
        for row in self._history:
            if row['vendor_name'] == args.vendor_name:
                if isinstance(row['invoice_total'], float):
                    raise ValueError('History money must not be float')
                totals[row['month']] += Decimal(row['invoice_total'])
        return limited([{'month': month, 'total': str(total)} for month, total in sorted(totals.items())])

    def related_invoices(self, vendor_name: str, start: date, end: date) -> dict:
        args = RelatedInvoicesArgs(vendor_name=vendor_name, start=start, end=end)
        matches = [i for i in self._invoices if i.vendor_name == args.vendor_name
                   and args.start <= i.invoice_date <= args.end]
        return limited([invoice_row(i) for i in sorted(matches, key=lambda i: (
            i.invoice_date, i.invoice_number, i.total, i.po_number or ''))])

    def duplicate_search(self, invoice_number: str) -> dict:
        args = DuplicateSearchArgs(invoice_number=invoice_number)
        base = next((index for index, i in enumerate(self._invoices) if i.invoice_number == args.invoice_number), None)
        if base is None:
            return limited([])
        original = self._invoices[base]
        matches = [i for index, i in enumerate(self._invoices) if index != base
                   and (i.invoice_number == args.invoice_number
                        or (i.vendor_name == original.vendor_name and i.total == original.total))]
        return limited([invoice_row(i) for i in sorted(matches, key=lambda i: (
            i.invoice_date, i.invoice_number, i.total, i.po_number or ''))])
