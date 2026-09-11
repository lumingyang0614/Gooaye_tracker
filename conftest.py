"""Repo-wide test safety.

`tests/conftest.py` only covers `tests/`, and this project has probe suites
living elsewhere (test_output/qa_company_changes/). Those drive the real
`rebuild_merged_views` against an in-memory stand-in for X Stock Tracker's
table — but the *merge bookkeeping* in app/services/merged_views.py writes to
Gooaye's own database, which those suites do not fake.

That combination is worse than it sounds. A full pass treats the sources it
can see as authoritative and prunes state for everything else, so running a
suite whose fake holds two companies deleted the recorded state for every real
one. Nothing was corrupted — the next pass rebuilt the rows mechanically — but
the fusions had to be redone, and a suite that costs model calls on a real
database is a trap to leave lying around.

So state lives in memory for anything running under pytest. It is a working
implementation rather than a no-op on purpose: the probe suites assert on
retry-after-failure and upgrade-after---no-llm, which are exactly the
behaviours a no-op would make vacuously true.
"""
from types import SimpleNamespace

import pytest

from app.services import merged_views


@pytest.fixture(autouse=True)
def merge_state_in_memory(monkeypatch):
    store: dict[str, SimpleNamespace] = {}

    def load_state():
        return dict(store)

    def save_state(written, fused, doomed, now):
        for symbol, sources in written.items():
            store[symbol] = SimpleNamespace(
                fingerprint=merged_views._fingerprint(sources), fused=symbol in fused)
        for symbol in doomed:
            store.pop(symbol, None)

    monkeypatch.setattr(merged_views, "_load_state", load_state)
    monkeypatch.setattr(merged_views, "_save_state", save_state)
    return store
