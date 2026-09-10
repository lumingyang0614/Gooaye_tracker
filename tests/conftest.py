"""Shared test fixtures.

The suite is meant to run in seconds and touch no network, no database and no
model. `app/services/llm.py` is the only module that deliberately waits — the
rate-limit backoff in `_post_with_retries` and the inter-request gap in
`paced` — and both are configured in seconds. Tests that drive the real sync
and company-narrative loops would otherwise pay those pauses for real: adding
the 2-second default took one existing functional test from under a second to
46, purely in sleeping.

So sleeping is stubbed for the whole suite and the requested delays are
recorded instead. Tests that care about the timing assert on `slept`; every
other test just runs fast.

Note this reaches further than it looks: `llm.asyncio` is the asyncio module
itself, so the patch replaces `asyncio.sleep` for the whole process, not just
for llm. A test that needs to hold a coroutine open must therefore use an
event it controls rather than a sleep — see
test_background_updates.py::test_concurrent_run_now_is_refused_rather_than_doubling_the_work.
"""
import pytest

from app.services import llm


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record every delay llm asks for, without actually waiting for it."""
    delays: list[float] = []

    async def fake_sleep(seconds):
        delays.append(seconds)

    monkeypatch.setattr(llm.asyncio, "sleep", fake_sleep)
    return delays
