"""The background scheduler that makes "Gooaye 有更新就寫進 xstocktracker"
automatic.

Everything the jobs call is stubbed: these test the scheduling and error
containment around the work, not the work itself (sync is covered by
test_sync_logic.py, the push by test_xstock_push.py).
"""
import asyncio

import pytest

from app.services import background_updates as module
from app.services.background_updates import BackgroundUpdates


@pytest.fixture
def schedules(monkeypatch):
    """Override config/settings.yaml's schedules block for one test."""
    def _set(**values):
        monkeypatch.setattr(module, "load_runtime_config", lambda: {"schedules": values})
    return _set


def _stub(monkeypatch, *, sync=None, push=None, securities=None, rebuild=None):
    async def default_sync(limit=None):
        return {"affected_symbols": ["2330.TW"], "episodes_analyzed": 1}
    async def default_securities():
        return {"total": 1976}
    async def default_rebuild(selected_symbols=None, use_llm=False):
        return {"symbols_updated": 1}
    monkeypatch.setattr(module, "sync_gooaye", sync or default_sync)
    monkeypatch.setattr(module, "push_company_insights", push or (lambda selected_symbols=None: {"pushed": 0}))
    monkeypatch.setattr(module, "sync_taiwan_securities", securities or default_securities)
    monkeypatch.setattr(module, "rebuild_company_insights", rebuild or default_rebuild)


# --- the episode job ---------------------------------------------------------

def test_episode_refresh_relies_on_sync_to_push_when_it_found_work(monkeypatch, schedules):
    """sync_gooaye already pushes the companies it touched; pushing again here
    would be a redundant second write of the same rows."""
    schedules()
    pushes = []
    _stub(monkeypatch, push=lambda selected_symbols=None: pushes.append(selected_symbols) or {"pushed": 1})
    result = asyncio.run(BackgroundUpdates().refresh_episodes())

    assert result["affected_symbols"] == ["2330.TW"]
    assert pushes == []


def test_episode_refresh_retries_the_push_when_nothing_changed(monkeypatch, schedules):
    """A quiet run is exactly when an earlier failed push gets reconciled: the
    insights are already in Gooaye's database, just not in the other one yet."""
    schedules()
    pushes = []

    async def quiet_sync(limit=None):
        return {"affected_symbols": [], "episodes_analyzed": 0}

    _stub(monkeypatch, sync=quiet_sync,
          push=lambda selected_symbols=None: pushes.append(selected_symbols) or {"pushed": 9})
    result = asyncio.run(BackgroundUpdates().refresh_episodes())

    assert pushes == [None]  # None == full push, the reconciling kind
    assert result["xstocktracker_push"] == {"pushed": 9}


def test_episode_limit_comes_from_config(monkeypatch, schedules):
    schedules(episode_sync_limit=3)
    seen = {}

    async def recording_sync(limit=None):
        seen["limit"] = limit
        return {"affected_symbols": ["NVDA"]}

    _stub(monkeypatch, sync=recording_sync)
    asyncio.run(BackgroundUpdates().refresh_episodes())
    assert seen["limit"] == 3


def test_blank_episode_limit_defers_to_the_sync_default(monkeypatch, schedules):
    # Empty means "use sync.incremental_new_limit", which sync_gooaye applies
    # when handed None — not "analyze zero episodes".
    schedules(episode_sync_limit="")
    seen = {}

    async def recording_sync(limit=None):
        seen["limit"] = limit
        return {"affected_symbols": []}

    _stub(monkeypatch, sync=recording_sync)
    asyncio.run(BackgroundUpdates().refresh_episodes())
    assert seen["limit"] is None


# --- the securities job ------------------------------------------------------

def test_security_refresh_rebuilds_and_pushes_without_calling_the_llm(monkeypatch, schedules):
    """A refreshed master is what turns a stored 「文曄」 into 3036.TW, but that
    is mechanical regrouping — paying for model calls here would be wrong."""
    schedules()
    calls = {}

    async def recording_rebuild(selected_symbols=None, use_llm=False):
        calls["use_llm"] = use_llm
        return {"symbols_updated": 4}

    _stub(monkeypatch, rebuild=recording_rebuild,
          push=lambda selected_symbols=None: {"pushed": 4})
    result = asyncio.run(BackgroundUpdates().refresh_taiwan_securities())

    assert calls["use_llm"] is False
    assert result["securities"] == {"total": 1976}
    assert result["xstocktracker_push"] == {"pushed": 4}


# --- status and failure containment -----------------------------------------

def test_run_now_reports_failure_in_status_instead_of_raising(monkeypatch, schedules):
    schedules()
    updater = BackgroundUpdates()

    async def failing():
        raise RuntimeError("transcript site unreachable")

    monkeypatch.setattr(updater, "refresh_episodes", failing)
    result = asyncio.run(updater.run_episodes_now())

    assert result["state"] == "error"
    assert "transcript site unreachable" in result["error"]
    assert updater.status["episode_sync"]["state"] == "error"


def test_run_now_records_a_successful_result(monkeypatch, schedules):
    schedules()
    _stub(monkeypatch)
    updater = BackgroundUpdates()
    result = asyncio.run(updater.run_episodes_now())

    assert updater.status["episode_sync"]["state"] == "ok"
    assert result["episodes_analyzed"] == 1


def test_concurrent_run_now_is_refused_rather_than_doubling_the_work(monkeypatch, schedules):
    schedules()
    updater = BackgroundUpdates()
    started = asyncio.Event()
    release = asyncio.Event()

    # Held open by an event rather than a sleep: conftest stubs asyncio.sleep
    # for the whole suite, so a sleep here would return instantly and the
    # first run would be finished before the second one was attempted.
    async def slow_sync(limit=None):
        started.set()
        await release.wait()
        return {"affected_symbols": []}

    _stub(monkeypatch, sync=slow_sync)

    async def scenario():
        first = asyncio.create_task(updater.run_episodes_now())
        await started.wait()
        second = await updater.run_episodes_now()
        release.set()
        await first
        return second

    # Two overlapping syncs would re-download the pack and could double-write
    # segments for the same episode.
    assert asyncio.run(scenario()) == {"state": "already_running"}


def test_disabled_schedules_start_no_tasks(schedules):
    schedules(enabled=False)
    updater = BackgroundUpdates()
    updater.start()

    assert updater.status == {"enabled": False, "episode_sync": {"state": "disabled"},
                              "taiwan_securities": {"state": "disabled"},
                              "merge_refresh": {"state": "disabled"}}
    assert updater._tasks == []


def test_individual_jobs_can_be_disabled(schedules):
    schedules(enabled=True, episode_sync_enabled=False)

    async def scenario():
        updater = BackgroundUpdates()
        updater.start()
        try:
            assert updater.status["episode_sync"] == {"state": "disabled"}
            assert updater.status["taiwan_securities"]["state"] == "waiting"
            assert updater.status["merge_refresh"]["state"] == "waiting"
            assert len(updater._tasks) == 2
        finally:
            await updater.stop()

    asyncio.run(scenario())


def test_interval_has_a_floor_so_a_typo_cannot_hammer_the_source(schedules):
    # These sources are third-party sites this project does not own; a
    # 0-hour interval must not become a tight download loop.
    schedules(enabled=True, episode_sync_hours=0, taiwan_security_sync_enabled=False)

    async def scenario():
        updater = BackgroundUpdates()
        updater.start()
        try:
            assert updater.status["episode_sync"]["interval_seconds"] == 3600
        finally:
            await updater.stop()

    asyncio.run(scenario())


def test_stop_cancels_every_task(schedules):
    schedules(enabled=True, startup_delay_seconds=999)

    async def scenario():
        updater = BackgroundUpdates()
        updater.start()
        assert len(updater._tasks) == 3
        await updater.stop()
        return updater

    updater = asyncio.run(scenario())
    assert updater._tasks == []


# --- the merge-refresh job ---------------------------------------------------

def test_merge_refresh_runs_a_full_pass(monkeypatch, schedules):
    """Scoping it to what this project touched would defeat the purpose: the
    job exists to catch rows the *other* tracker wrote on its own schedule,
    which nothing here has a symbol list for."""
    schedules()
    calls = []

    async def fake_rebuild(selected_symbols=None, *, use_llm=True, force=False):
        calls.append((selected_symbols, use_llm, force))
        return {"merged": 3, "unchanged": 50}

    monkeypatch.setattr(module, "rebuild_merged_views", fake_rebuild)
    result = asyncio.run(BackgroundUpdates().refresh_merged_views())

    assert calls == [(None, True, False)]
    assert result["unchanged"] == 50


def test_merge_refresh_can_be_disabled(schedules):
    schedules(enabled=True, merge_refresh_enabled=False)

    async def scenario():
        updater = BackgroundUpdates()
        updater.start()
        try:
            assert updater.status["merge_refresh"] == {"state": "disabled"}
            assert not any("merge" in task.get_name() for task in updater._tasks)
        finally:
            await updater.stop()

    asyncio.run(scenario())


def test_merge_refresh_interval_has_a_floor(schedules):
    """Each tick is two queries plus any fusion it finds work for; a 0-hour
    typo would sit in a loop against the other project's database."""
    schedules(enabled=True, merge_refresh_hours=0,
              episode_sync_enabled=False, taiwan_security_sync_enabled=False)

    async def scenario():
        updater = BackgroundUpdates()
        updater.start()
        try:
            assert updater.status["merge_refresh"]["interval_seconds"] == 3600
        finally:
            await updater.stop()

    asyncio.run(scenario())
