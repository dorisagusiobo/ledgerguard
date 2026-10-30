"""PDF generation and the extraction input boundary require no reader library."""
import importlib.util
from pathlib import Path
import re
import shutil

import pytest

from ledgerguard.models import Invoice
from ledgerguard.pdf import load_pdf_inputs, render_invoice


def pdf_text(data):
    # Core-font, uncompressed PDF literal strings, including escaped parentheses.
    literals = re.findall(rb'\(((?:\\.|[^\\)])*)\)\s*Tj', data)
    return '\n'.join(re.sub(rb'\\([\\()])', rb'\1', text).decode('latin-1') for text in literals)


@pytest.fixture(scope='module', params=[7, 8, 9])
def dataset(generated, request):
    return generated(request.param)


def test_every_invoice_has_one_page_pdf_with_stated_text(dataset):
    jsons = sorted((dataset / 'invoices').glob('*.json'))
    pdfs = sorted((dataset / 'invoices_pdf').glob('*.pdf'))
    assert len(jsons) == len(pdfs) == 57
    for path in jsons:
        invoice = Invoice.model_validate_json(path.read_text())
        data = (dataset / 'invoices_pdf' / (path.stem + '.pdf')).read_bytes()
        assert data.startswith(b'%PDF')
        assert b'/FlateDecode' not in data
        assert len(re.findall(rb'/Type /Page\b', data)) == 1
        text = pdf_text(data)
        assert invoice.vendor_name in text
        assert invoice.invoice_number in text
        assert invoice.invoice_date.isoformat() in text
        assert invoice.due_date.isoformat() in text
        assert (invoice.po_number or 'Not provided') in text
        for item in invoice.line_items:
            assert item.description in text
            # PDF extraction emits each table cell separately. Join the actual
            # page cells in reading order, not values taken from the JSON.
            assert item.source.text in ' | '.join(text.splitlines())
            assert item.source.page == 1
        assert f'{invoice.subtotal:.2f}' in text
        assert f'{invoice.tax:.2f}' in text
        assert f'{invoice.total:.2f}' in text
        assert data == render_invoice(invoice)


def test_pdf_bytes_deterministic_across_creation_times(dataset, monkeypatch):
    import ledgerguard.pdf as module

    original = module.FPDF.set_creation_date
    calls = []

    def fixed_date(pdf, value):
        calls.append(value)
        return original(pdf, value)

    monkeypatch.setattr(module.FPDF, 'set_creation_date', fixed_date)
    invoice = Invoice.model_validate_json(next((dataset / 'invoices').glob('*.json')).read_text())
    assert render_invoice(invoice) == render_invoice(invoice)
    assert calls == [module.CREATION_DATE, module.CREATION_DATE]


def test_guard_passes_all_pdf_text(dataset):
    spec = importlib.util.spec_from_file_location(
        'guard', Path(__file__).resolve().parents[1] / '.claude/hooks/checks.py')
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    for path in (dataset / 'invoices_pdf').glob('*.pdf'):
        assert not guard.suspected_financial_data(pdf_text(path.read_bytes()))


def test_pdf_loader_cannot_read_json_or_answers(dataset, tmp_path, monkeypatch):
    source = tmp_path / 'inputs'
    shutil.copytree(dataset / 'invoices_pdf', source / 'invoices_pdf')
    original_read = Path.read_bytes

    def only_pdf(path):
        assert path.parent == source / 'invoices_pdf'
        assert path.suffix == '.pdf'
        return original_read(path)

    def no_text(*args, **kwargs):
        raise AssertionError('Extractor input loader must never read JSON or text answers')

    monkeypatch.setattr(Path, 'read_bytes', only_pdf)
    monkeypatch.setattr(Path, 'read_text', no_text)
    assert len(load_pdf_inputs(source)) == 57
    assert not (source / 'invoices').exists()
    assert not (source / 'answer_key').exists()


def test_pdf_loader_refuses_redirect_to_json(tmp_path):
    (tmp_path / 'invoices_pdf').mkdir()
    target = tmp_path / 'answer.json'
    target.write_text('{}')
    (tmp_path / 'invoices_pdf' / 'INV-2026-0001.pdf').symlink_to(target)
    with pytest.raises(ValueError, match='symlinks'):
        load_pdf_inputs(tmp_path)
