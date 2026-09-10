"""Surviving a provider rate limit (429).

A real 17-company run against BAI produced 8 consecutive 429s and finished
with narratives for 2 companies out of 17; the other 15 silently kept the
mechanical fallback thesis. Two defences, tested here:

* `_post_with_retries` waits the limit out instead of raising on the spot.
* `llm.paced` spaces out both bulk loops (episodes, company batches) so a run
  is less likely to provoke the limit at all.

No sleeping actually happens: the autouse `slept` fixture in conftest.py
stubs it for the whole suite and records the requested delays, so these tests
can assert on the timing without paying it.
"""
import asyncio

import httpx
import pytest

from app.services import company_insights
from app.services.llm import _post_with_retries, _retry_delay, paced


class FakeResponses:
    """Replays a scripted sequence of status codes, recording each request."""

    def __init__(self, statuses, headers=None):
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.calls = 0

    async def post(self, url, json=None, headers=None):
        self.calls += 1
        status = self.statuses[min(self.calls - 1, len(self.statuses) - 1)]
        return httpx.Response(
            status, json={"choices": [{"message": {"content": "{}"}}]},
            headers=self.headers if status == 429 else {},
            request=httpx.Request("POST", url),
        )


def _post(client, **kwargs):
    options = {"max_retries": 4, "base_backoff": 5.0, "max_backoff": 60.0}
    options.update(kwargs)
    return asyncio.run(_post_with_retries(client, "http://test/chat/completions", {}, {}, **options))


# --- the retry loop ----------------------------------------------------------

def test_a_rate_limited_request_is_retried_until_it_succeeds(slept):
    client = FakeResponses([429, 429, 200])
    response = _post(client)
    assert response.status_code == 200
    assert client.calls == 3
    assert len(slept) == 2


def test_success_on_the_first_try_never_sleeps(slept):
    client = FakeResponses([200])
    assert _post(client).status_code == 200
    assert client.calls == 1 and slept == []


def test_persistent_rate_limiting_returns_the_real_status_to_the_caller(slept):
    # The caller still calls raise_for_status(); it must see the true 429
    # rather than a masked success, so the failure is reported honestly.
    client = FakeResponses([429])
    response = _post(client, max_retries=2)
    assert response.status_code == 429
    assert client.calls == 3  # the original plus two retries


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_server_errors_are_retried_too(slept, status):
    client = FakeResponses([status, 200])
    assert _post(client).status_code == 200
    assert client.calls == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(slept, status):
    """A bad key or malformed request fails identically on a retry; retrying
    only delays an error the operator needs to see."""
    client = FakeResponses([status])
    assert _post(client).status_code == status
    assert client.calls == 1 and slept == []


def test_retries_can_be_switched_off(slept):
    client = FakeResponses([429])
    assert _post(client, max_retries=0).status_code == 429
    assert client.calls == 1 and slept == []


# --- how long to wait --------------------------------------------------------

def _response(headers):
    return httpx.Response(429, headers=headers, request=httpx.Request("POST", "http://test"))


def test_retry_after_header_is_obeyed_rather_than_guessed():
    # The provider is stating when it will accept traffic again; a shorter
    # self-chosen backoff just burns another request against the same limit.
    assert _retry_delay(_response({"Retry-After": "17"}), 1, 5.0, 60.0) == 17.0


def test_retry_after_is_capped_so_one_header_cannot_stall_the_run():
    assert _retry_delay(_response({"Retry-After": "99999"}), 1, 5.0, 60.0) == 60.0


def test_http_date_retry_after_is_understood():
    # RFC 7231 allows an HTTP-date instead of a delay in seconds.
    delay = _retry_delay(_response({"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}), 1, 5.0, 60.0)
    assert delay == 60.0


def test_a_past_retry_after_date_does_not_produce_a_negative_delay():
    delay = _retry_delay(_response({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}), 1, 5.0, 60.0)
    assert delay == 0.0


def test_unparseable_retry_after_falls_back_to_backoff():
    delay = _retry_delay(_response({"Retry-After": "soon-ish"}), 1, 5.0, 60.0)
    assert 2.5 <= delay <= 5.0


def test_backoff_grows_with_each_attempt():
    without_header = {}
    first = [_retry_delay(_response(without_header), 1, 5.0, 60.0) for _ in range(20)]
    third = [_retry_delay(_response(without_header), 3, 5.0, 60.0) for _ in range(20)]
    assert max(first) <= min(third)


def test_backoff_is_jittered_so_parallel_callers_do_not_retry_in_lockstep():
    delays = {_retry_delay(_response({}), 2, 5.0, 60.0) for _ in range(30)}
    assert len(delays) > 1


def test_backoff_never_exceeds_the_cap():
    assert all(_retry_delay(_response({}), 10, 5.0, 30.0) <= 30.0 for _ in range(20))


# --- pacing the company narrative pass --------------------------------------

def test_company_batches_are_spaced_out(monkeypatch, slept):
    """Nine back-to-back batches is what provoked the limit; pause between."""
    calls = []

    async def fake_analyze(blocks, max_tokens):
        calls.append(sorted(blocks))
        return {symbol: {"thesis": "t", "strengths": [], "weaknesses": []} for symbol in blocks}

    monkeypatch.setattr(company_insights, "analyze_companies", fake_analyze)
    monkeypatch.setattr(company_insights, "SessionLocal", _no_session())
    symbols = ["A", "B", "C", "D", "E"]
    grouped = {symbol: [_segment()] for symbol in symbols}
    config = {"company_analysis_batch_size": 2, "company_analysis_pause_seconds": 2.0}

    asyncio.run(company_insights._write_company_narratives(symbols, grouped, config))

    assert len(calls) == 3          # 5 symbols / batch of 2
    assert slept == [2.0, 2.0]      # paused between batches, not before the first


def test_pause_can_be_disabled(monkeypatch, slept):
    async def fake_analyze(blocks, max_tokens):
        return {symbol: {"thesis": "t", "strengths": [], "weaknesses": []} for symbol in blocks}

    monkeypatch.setattr(company_insights, "analyze_companies", fake_analyze)
    monkeypatch.setattr(company_insights, "SessionLocal", _no_session())
    grouped = {s: [_segment()] for s in ["A", "B"]}
    asyncio.run(company_insights._write_company_narratives(
        ["A", "B"], grouped, {"company_analysis_batch_size": 1, "company_analysis_pause_seconds": 0}))
    assert slept == []


def _segment():
    from types import SimpleNamespace
    return SimpleNamespace(
        episode=SimpleNamespace(episode_number=1, source_url=""),
        chunk_index=0, importance=0.5, summary="摘要", sentiment="bullish", reason="理由",
    )


def _no_session():
    """A SessionLocal stand-in whose writes go nowhere (no database here)."""
    class Session:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def scalar(self, *_): return None
        def commit(self): pass
    return Session


# --- the shared pacing helper -----------------------------------------------
# Both bulk loops (episodes in sync.py, company batches in company_insights.py)
# now go through llm.paced, so these exercise the real code both call.

def test_paced_sleeps_between_items_but_not_around_the_edges(slept):
    async def drain():
        return [item async for item in paced(["a", "b", "c"], 2.0)]

    assert asyncio.run(drain()) == ["a", "b", "c"]
    assert slept == [2.0, 2.0]      # 3 items => 2 pauses


def test_paced_never_sleeps_for_a_single_item(slept):
    async def drain():
        return [item async for item in paced(["only"], 5.0)]

    assert asyncio.run(drain()) == ["only"]
    assert slept == []


def test_paced_with_zero_pause_does_not_sleep(slept):
    async def drain():
        return [item async for item in paced(range(4), 0)]

    assert asyncio.run(drain()) == [0, 1, 2, 3]
    assert slept == []


def test_paced_handles_an_empty_run(slept):
    async def drain():
        return [item async for item in paced([], 2.0)]

    assert asyncio.run(drain()) == [] and slept == []


def test_episode_loop_uses_the_shared_pacing_helper():
    """sync.py's episode loop is what collected 429s on EP676/677/678."""
    import inspect
    from app.services import sync as sync_module
    source = inspect.getsource(sync_module.sync_gooaye)
    assert "paced(" in source
    assert "episode_analysis_pause_seconds" in source


def test_company_batch_loop_uses_the_shared_pacing_helper():
    import inspect
    source = inspect.getsource(company_insights._write_company_narratives)
    assert "paced(" in source
    assert "company_analysis_pause_seconds" in source
