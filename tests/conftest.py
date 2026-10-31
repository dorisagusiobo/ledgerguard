"""Shared generated inputs. Copy before mutating a cached dataset."""
import pytest

from ledgerguard.synthetic import generate


@pytest.fixture(scope='session')
def generated(tmp_path_factory):
    cache = {}

    def get(seed):
        if seed not in cache:
            out = tmp_path_factory.mktemp(f'seed-{seed}') / 'data'
            generate(seed, out)
            cache[seed] = out
        return cache[seed]

    return get


@pytest.fixture
def pipeline_case(generated):
    import json
    from ledgerguard.pdf import load_pdf_inputs
    from ledgerguard.rules import load_inputs

    root = generated(7)
    inputs = load_inputs(root)
    splits = json.loads((root / 'splits.json').read_text())
    answers = json.loads((root / 'answer_key/expected_findings.json').read_text())
    invoices = [i for i in inputs.invoices if splits[i.invoice_number] == 'dev']
    all_pdfs = load_pdf_inputs(root)
    pdfs = {n: b for n, b in all_pdfs.items() if splits[n] == 'dev'}
    return root, pdfs, invoices, answers, splits


@pytest.fixture
def case(pipeline_case):
    return pipeline_case
