"""Periodic background jobs, so a running Gooaye Tracker picks up new 股癌
episodes on its own instead of waiting for someone to POST /api/sync.

Two independent loops, each with its own interval and its own lock:

* `episode_sync` runs the full pipeline in app/services/sync.py — fetch the
  transcript pack and RSS feed, analyze what is new, rebuild the affected
  company insights, and push those companies into X Stock Tracker's
  x_company_views. That last step is what makes "Gooaye 有更新就寫進
  xstocktracker" automatic rather than manual.
* `taiwan_securities` refreshes the TWSE/TPEx listing table daily, so a newly
  listed company resolves to a real ticker rather than being stored as the raw
  Chinese name the model produced.

Modeled on X Stock Tracker's app/services/background_updates.py so both
projects' schedulers behave and report identically (same status shape, same
`/api/schedules/status` contract), while staying separate code in separate
projects — neither imports the other.

A job that raises is logged and reported in `status`, then retried on the next
tick; it never takes the loop or the API process down with it.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from app.runtime_config import load_runtime_config
from app.services.company_insights import rebuild_company_insights
from app.services.sync import sync_gooaye
from app.services.taiwan_securities import sync_taiwan_securities
from app.services.xstock_push import push_company_insights

logger = logging.getLogger("uvicorn.error")

UpdateCallable = Callable[[], Awaitable[dict[str, Any]]]


class BackgroundUpdates:
    """Periodically sync new 股癌 episodes and refresh the security master."""

    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._episode_lock = asyncio.Lock()
        self._securities_lock = asyncio.Lock()
        self.status: dict[str, Any] = {
            "enabled": False,
            "episode_sync": {"state": "waiting"},
            "taiwan_securities": {"state": "waiting"},
        }

    def start(self) -> None:
        config = load_runtime_config().get("schedules", {})
        if not config.get("enabled", True):
            self.status = {
                "enabled": False,
                "episode_sync": {"state": "disabled"},
                "taiwan_securities": {"state": "disabled"},
            }
            logger.info("背景排程未啟動 | reason=schedules.enabled=false")
            return
        if self._tasks:
            logger.warning("背景排程已在執行，略過重複啟動 | tasks=%d", len(self._tasks))
            return
        if self._stop.is_set():
            self._stop = asyncio.Event()

        initial_delay = max(0, int(config.get("startup_delay_seconds", 10)))
        self.status["enabled"] = True
        self.status["startup_delay_seconds"] = initial_delay

        # Floors, not just defaults: a misconfigured 1-minute episode loop would
        # re-download the whole transcript pack (and possibly re-run Whisper)
        # continuously, against a third-party site this project does not own.
        jobs = (
            ("episode_sync", "episode_sync_enabled", "episode_sync_hours", 6, 3600, self.refresh_episodes),
            ("taiwan_securities", "taiwan_security_sync_enabled", "taiwan_security_sync_hours", 24, 3600, self.refresh_taiwan_securities),
        )
        for name, enabled_key, hours_key, default_hours, floor_seconds, operation in jobs:
            if not config.get(enabled_key, True):
                self.status[name] = {"state": "disabled"}
                logger.info("背景排程工作已停用 | job=%s | reason=schedules.%s=false", name, enabled_key)
                continue
            interval = max(floor_seconds, int(config.get(hours_key, default_hours)) * 3600)
            self.status[name] = {"state": "waiting", "interval_seconds": interval}
            self._tasks.append(asyncio.create_task(
                self._loop(name, operation, interval, initial_delay),
                name=f"gooaye-{name.replace('_', '-')}",
            ))
            logger.info("背景排程已啟動 | job=%s | initial_delay=%ds | interval=%ds",
                        name, initial_delay, interval)

    async def stop(self) -> None:
        logger.info("正在停止背景排程 | tasks=%d", len(self._tasks))
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        logger.info("背景排程已停止")

    async def _loop(self, name: str, operation: UpdateCallable, interval_seconds: int, initial_delay: int) -> None:
        logger.info("排程迴圈等待首次執行 | job=%s | delay=%ds", name, initial_delay)
        if await self._wait(initial_delay):
            return
        while not self._stop.is_set():
            await self._run(name, operation)
            next_run = datetime.now(timezone.utc) + timedelta(seconds=interval_seconds)
            self.status[name]["next_run_at"] = next_run.isoformat()
            logger.info("排程等待下次執行 | job=%s | next_run_at=%s | interval=%ds",
                        name, next_run.isoformat(), interval_seconds)
            if await self._wait(interval_seconds):
                return

    async def _wait(self, seconds: int) -> bool:
        """Sleep, returning True if the scheduler was asked to stop meanwhile."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _run(self, name: str, operation: UpdateCallable) -> dict[str, Any] | None:
        started_at = datetime.now(timezone.utc)
        self.status[name] = {**self.status.get(name, {}), "state": "running",
                             "started_at": started_at.isoformat()}
        logger.info("排程工作開始 | job=%s | started_at=%s", name, started_at.isoformat())
        try:
            result = await operation()
            self.status[name] = {
                **self.status.get(name, {}), "state": "ok",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(), "result": result,
            }
            logger.info("排程工作完成 | job=%s | result=%s", name, result)
            return result
        except asyncio.CancelledError:
            logger.info("排程工作已取消 | job=%s", name)
            raise
        except Exception as exc:
            self.status[name] = {
                **self.status.get(name, {}), "state": "error",
                "started_at": started_at.isoformat(),
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            logger.exception("排程工作失敗；下個週期將重試 | job=%s | error=%s", name, exc)
            return None

    async def run_episodes_now(self) -> dict[str, Any]:
        if self._episode_lock.locked():
            logger.warning("集數同步已在執行，拒絕重複工作")
            return {"state": "already_running"}
        return await self._run("episode_sync", self.refresh_episodes) or self.status["episode_sync"]

    async def refresh_episodes(self) -> dict[str, Any]:
        """One scheduled pass of the full pipeline.

        sync_gooaye already rebuilds company insights for the episodes it
        analyzed and pushes them to X Stock Tracker, so nothing extra is done
        here when it found work. When it found none, the push is still worth
        attempting: a previous run may have written insights while the other
        database was unreachable, and this is what eventually reconciles them.
        """
        async with self._episode_lock:
            config = load_runtime_config().get("schedules", {})
            raw_limit = config.get("episode_sync_limit")
            limit = int(raw_limit) if raw_limit not in (None, "") else None
            logger.info("自動檢查新集數 | limit=%s", limit if limit is not None else "sync.incremental_new_limit")
            result = await sync_gooaye(limit=limit)
            if not result.get("affected_symbols"):
                logger.info("本次沒有新的公司觀點 | 改為重試 xstocktracker 推送以補齊先前失敗的更新")
                result["xstocktracker_push"] = push_company_insights()
            return result

    async def refresh_taiwan_securities(self) -> dict[str, Any]:
        """Refresh the TWSE/TPEx master, then re-derive company insights from it.

        The rebuild is the point of the refresh: a mention stored as 「文曄」
        only becomes 3036.TW once the master knows the name, and nothing
        re-reads the master on its own. It runs without the LLM — this is a
        mechanical regrouping, not new analysis — and the resulting rows are
        pushed on to X Stock Tracker like any other insight change.
        """
        async with self._securities_lock:
            logger.info("自動同步 TWSE／TPEx 證券主檔")
            securities = await sync_taiwan_securities()
            insights = await rebuild_company_insights(use_llm=False)
            return {"securities": securities, "company_insights": insights,
                    "xstocktracker_push": push_company_insights()}


background_updates = BackgroundUpdates()
