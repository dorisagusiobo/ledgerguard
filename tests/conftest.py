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
