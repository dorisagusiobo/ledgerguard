"""Deterministic, single-page invoice PDFs and PDF-only extractor inputs."""
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF
from fpdf.fonts import FontFace

from ledgerguard.models import Invoice

CREATION_DATE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def line_item_cells(description, quantity, unit_price, amount) -> tuple[str, str, str, str]:
    """Canonical row values shared by source evidence and the rendered table."""
    return description, str(quantity), str(unit_price), f'{amount:.2f}'


def line_item_source_text(description, quantity, unit_price, amount) -> str:
    """Represent exact table cells in reading order with explicit separators."""
    return ' | '.join(line_item_cells(description, quantity, unit_price, amount))


def render_invoice(invoice: Invoice) -> bytes:
    """Render stated values faithfully, including planted arithmetic mistakes.

    Core Helvetica fonts support Latin-1. The generator uses ASCII hyphens;
    unsupported characters and invoices too large for one page fail explicitly.
    """
    pdf = FPDF(format='A4')
    pdf.set_creation_date(CREATION_DATE)
    pdf.set_compression(False)
    pdf.set_margins(18, 18, 18)
    pdf.set_auto_page_break(True, margin=18)
    pdf.add_page()
    pdf.set_font('Helvetica', 'B', 19)
    pdf.set_text_color(25, 45, 65)
    pdf.multi_cell(0, 10, invoice.vendor_name, new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', 'B', 12)
    pdf.cell(0, 10, 'INVOICE', new_x='LMARGIN', new_y='NEXT')
    pdf.set_text_color(30, 30, 30)
    pdf.set_font('Helvetica', size=10)
    fields = [('Invoice number', invoice.invoice_number),
              ('Invoice date', invoice.invoice_date.isoformat()),
              ('Due date', invoice.due_date.isoformat()),
              ('PO number', invoice.po_number or 'Not provided'),
              ('Currency', invoice.currency)]
    for label, value in fields:
        pdf.multi_cell(0, 7, f'{label}: {value}', new_x='LMARGIN', new_y='NEXT')
    pdf.ln(7)
    with pdf.table(col_widths=(90, 20, 32, 32), line_height=8,
                   text_align=('LEFT', 'RIGHT', 'RIGHT', 'RIGHT'),
                   headings_style=FontFace(emphasis='BOLD', fill_color=(233, 239, 244))) as table:
        row = table.row()
        for heading in ['Description', 'Quantity', 'Unit price', 'Amount']:
            row.cell(heading)
        for item in invoice.line_items:
            row = table.row()
            for value in line_item_cells(item.description, item.quantity, item.unit_price, item.amount):
                row.cell(value)
    pdf.ln(8)
    for label, amount in [('Subtotal', invoice.subtotal), ('Tax', invoice.tax), ('Total', invoice.total)]:
        pdf.set_font('Helvetica', 'B' if label == 'Total' else '', 11)
        pdf.cell(142, 9, label, align='R')
        pdf.cell(32, 9, f'{amount:.2f}', align='R', new_x='LMARGIN', new_y='NEXT')
    if pdf.pages_count != 1:
        raise ValueError('Invoice does not fit the single-page PDF layout')
    return bytes(pdf.output())


def load_pdf_inputs(path: Path) -> dict[str, bytes]:
    """Future extractor input boundary: reads invoices_pdf/*.pdf only.

    JSON invoices and answer keys belong to evaluation, never extraction inputs.
    Symlinks are refused so a PDF filename cannot redirect to a JSON answer.
    """
    directory = Path(path) / 'invoices_pdf'
    if not directory.is_dir():
        raise ValueError('Missing invoices_pdf directory')
    result = {}
    for file in sorted(directory.glob('*.pdf')):
        if file.is_symlink():
            raise ValueError('PDF input symlinks are not allowed')
        data = file.read_bytes()
        if not data.startswith(b'%PDF'):
            raise ValueError(f'Invalid PDF input: {file.name}')
        result[file.stem] = data
    return result
