"""Fuse both trackers' views of a company into one, with the LLM.

`x_company_views` holds one row per source — Gooaye's 股癌 view and X Stock
Tracker's analyst-community view — which is right for the dashboards but
wrong for a consumer that asks "what's the view on MU?" and expects a single
answer. Pasting the two texts together is not an answer either: it just makes
the reader do the integration.

So this writes a third row, `source='merged'`, holding one genuinely merged
opinion per company: where the two sides agree it says so once, where they
conflict it says what the conflict is. Consumers read only the merged rows
(see app/services/xstock_unified_view.py) and never see the split.

Deliberately *not* done here: any re-weighting of the two sources. X Stock
Tracker applies its own source weighting (the Serenity playbook bonus in its
`analysis.priority_weight`) before it writes its row, so that judgement is
already baked into what arrives here; re-weighting again would double-count
it. Gooaye's row is not weighted the same way — its stance is an unweighted
majority vote over segment sentiments and its `confidence` is mean segment
importance, which app/services/company_insights.py is explicit is *not* a
confidence in the stance. The merged `confidence` is the mean of the two,
which assumes the columns are roughly comparable; that assumption has not
been checked against X Stock Tracker's definition, so merged confidence is
indicative rather than calibrated.

Cost is kept proportional to the actual work. Only companies that really have
two views cost a model call, and only when something changed since the last
fusion: a company whose merged row is already newer than both its sources is
left untouched, so a periodic full rebuild is nearly free and exists mainly to
pick up changes the other tracker made on its own.

Nothing here may take a caller down. The target database belongs to the other
project and can be absent, unmigrated or simply down; every failure becomes a
reported result, never an exception — the same contract as
app/services/xstock_push.py, because by the time this runs the caller's own
sync has already succeeded and committed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select, text

from app.db import SessionLocal, init_db
from app.models import MergedViewState
from app.runtime_config import load_runtime_config
from app.services.llm import paced, synthesize_merged_views
from app.services.securities import xstocktracker_engine
from app.services.xstock_push import SOURCE_GOOAYE, TABLE, _json_list

logger = logging.getLogger("uvicorn.error")

SOURCE_MERGED = "merged"
SOURCE_X = "x"

#: The sources this module knows how to fuse, in the order they are shown to
#: the model. Anything else in the table is ignored rather than counted as a
#: second opinion: `len(sources) > 1` used to be enough to pay for a model
#: call, so an unrecognised source bought a call that then sent one view.
FUSABLE_SOURCES = (SOURCE_GOOAYE, SOURCE_X)

# Deliberately anonymous. The model needs the two views kept apart to notice
# that they disagree, and nothing more than that — naming them was asking for
# the leak it produced: told not to mention sources while being handed
# "《股癌》Podcast 逐字稿分析" and "X 分析師社群彙總", it wrote theses like
# "X社群單一貼文指其調漲報價…而逐字稿分析多持保留態度". A reader of the
# merged row is owed a judgement about the company, not about who said what.
_LABELS = {SOURCE_GOOAYE: "觀點A", SOURCE_X: "觀點B"}

# app/services/xstock_push.py stamps its rows with ［股癌 Gooaye · US · 提及 3 集］
# so they stay identifiable in X Stock Tracker's own dashboard. The merged row
# is a view of the company rather than of a tracker, so that header is stripped
# both from what the model sees and from the mechanical fallback text.
_PROVENANCE = re.compile(r"^［股癌 Gooaye[^］]*］\s*")

# Words that mean the fusion described who said something rather than what is
# true of the company. The prompt forbids them; this is how anyone finds out
# when the model ignored it, which is not visible by reading the row — it is
# fluent, plausible prose either way.
_SOURCE_LEAK = re.compile(
    r"觀點[AB]|股癌|Podcast|逐字稿|社群|貼文|分析師|X\s*上的", re.IGNORECASE)


def source_leak(thesis: str) -> str:
    """The first source-naming phrase in a fused thesis, or ''."""
    match = _SOURCE_LEAK.search(thesis or "")
    return match.group(0) if match else ""

_SELECT = text(f"""
    SELECT source, upper(btrim(symbol)) AS symbol, company_name, stance, confidence,
           thesis, strengths_json, weaknesses_json, evidence_urls_json, mention_count,
           updated_at
    FROM {TABLE}
    WHERE source = ANY(:sources)
""")

_SELECT_MERGED = text(f"""
    SELECT upper(btrim(symbol)) AS symbol, updated_at
    FROM {TABLE}
    WHERE source = :merged
""")

_DELETE = text(f"""
    DELETE FROM {TABLE}
    WHERE source = :merged AND upper(btrim(symbol)) = ANY(:symbols)
""")

_UPSERT = text(f"""
    INSERT INTO {TABLE} (
        source, symbol, company_name, mention_count, stance, confidence,
        thesis, strengths_json, weaknesses_json, evidence_urls_json, updated_at
    ) VALUES (
        :source, :symbol, :company_name, :mention_count, :stance, :confidence,
        :thesis, :strengths_json, :weaknesses_json, :evidence_urls_json, :updated_at
    )
    ON CONFLICT (source, symbol) DO UPDATE SET
        company_name = EXCLUDED.company_name,
        mention_count = EXCLUDED.mention_count,
        stance = EXCLUDED.stance,
        confidence = EXCLUDED.confidence,
        thesis = EXCLUDED.thesis,
        strengths_json = EXCLUDED.strengths_json,
        weaknesses_json = EXCLUDED.weaknesses_json,
        evidence_urls_json = EXCLUDED.evidence_urls_json,
        updated_at = EXCLUDED.updated_at
""")


def merge_config() -> dict[str, Any]:
    return load_runtime_config().get("xstocktracker", {}) or {}


def _parse(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _as_utc(value: Any) -> datetime | None:
    """Timestamps can come back naive depending on the column type."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _thesis_of(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    return _PROVENANCE.sub("", str(row.get("thesis") or "").strip()).strip()


def _load_sources(connection: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """symbol -> {source -> row}, for every company either tracker covers.

    The unknown-source check is deliberately duplicated here rather than left
    to the WHERE clause: `len(sources) > 1` downstream is what decides to spend
    a model call, and it must not be one edit to the SQL away from counting a
    source this module has no evidence builder for.
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in connection.execute(_SELECT, {"sources": list(FUSABLE_SOURCES)}):
        data = dict(row._mapping)
        symbol, source = data.get("symbol"), data.get("source")
        if symbol and source in FUSABLE_SOURCES:
            grouped.setdefault(symbol, {})[source] = data
    return grouped


def _load_merged(connection: Any) -> dict[str, datetime | None]:
    """symbol -> when its merged row was last written."""
    merged: dict[str, datetime | None] = {}
    for row in connection.execute(_SELECT_MERGED, {"merged": SOURCE_MERGED}):
        data = dict(row._mapping)
        symbol = data.get("symbol")
        if symbol:
            merged[symbol] = _as_utc(data.get("updated_at"))
    return merged


def _fingerprint(sources: dict[str, dict[str, Any]]) -> str:
    """Which source rows a merged row was built from, and which version of each.

    Comparing timestamps alone could not see a source being *removed*: the one
    left behind is older than the merged row, so a company whose second view
    had been withdrawn kept serving the two-sided thesis and the summed mention
    count indefinitely. Naming the sources makes that a difference.
    """
    parts = []
    for source in FUSABLE_SOURCES:
        row = sources.get(source)
        if row is None:
            continue
        stamp = _as_utc(row.get("updated_at"))
        parts.append(f"{source}@{stamp.isoformat() if stamp else '?'}")
    return "|".join(parts)


def _is_stale(
    sources: dict[str, dict[str, Any]],
    merged_at: datetime | None,
    record: Any,
    *,
    want_fusion: bool,
) -> bool:
    """Whether this company's merged row needs rewriting.

    Deliberately *not* "is the merged row newer than its sources". That test
    called a row current because of when it was written, which made every
    fallback self-confirming: a failed model call wrote its mechanical row
    stamped now, and from the next pass on the company looked up to date and
    was never retried. The same trap caught `--no-llm`, which could then never
    be upgraded by a later run with the model enabled.

    So the question asked here is what the last pass actually achieved:
    unrecorded, built from different sources, or fused-but-only-mechanically.
    """
    if merged_at is None:
        return True                       # no merged row in the shared table
    if record is None:
        return True                       # this project has no record of writing one
    if any(_as_utc(row.get("updated_at")) is None for row in sources.values()):
        return True                       # cannot prove it is current, so redo it
    if record.fingerprint != _fingerprint(sources):
        return True                       # a source changed, appeared or vanished
    if want_fusion and len(sources) > 1 and not record.fused:
        return True                       # a fusion that failed or was skipped
    return False


def _display_name(sources: dict[str, dict[str, Any]], symbol: str) -> str:
    """Prefer a name that is not just the ticker repeated, Gooaye's first."""
    for source in FUSABLE_SOURCES:
        name = str(sources.get(source, {}).get("company_name") or "").strip()
        if name and name.upper() != symbol:
            return name
    return symbol


def _combined_evidence(sources: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """The prompt block for one company, plus its merged evidence links."""
    blocks, urls = [], []
    for source in FUSABLE_SOURCES:
        row = sources.get(source)
        if not row:
            continue
        lines = [f"[{_LABELS[source]}]"]
        stamp = _as_utc(row.get("updated_at"))
        if stamp:
            # Labelled for what it actually is. This is when the tracker last
            # rebuilt its aggregate, not when the underlying episode or post
            # happened, so it is an upper bound on freshness and nothing more —
            # calling it 資料時間 invited the model to read a recent rebuild of
            # old material as recent evidence.
            lines.append(f"彙整更新於：{stamp.date().isoformat()}（非原始發言時間）")
        lines.append(f"目前立場：{row['stance']}")
        thesis = _thesis_of(row)
        if thesis:
            lines.append(f"論點：{thesis}")
        strengths, weaknesses = _parse(row.get("strengths_json")), _parse(row.get("weaknesses_json"))
        if strengths:
            lines.append("正面理由：" + "；".join(strengths))
        if weaknesses:
            lines.append("風險：" + "；".join(weaknesses))
        blocks.append("\n".join(lines))
        urls.extend(_parse(row.get("evidence_urls_json")))
    return "\n\n".join(blocks), list(dict.fromkeys(urls))


def _mechanical_row(symbol: str, sources: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Copy a single-source company straight through — nothing to fuse.

    Also the fallback shape for a company whose model call failed: the merged
    row still exists and is readable, just without a fused narrative.

    The text comes from whichever sources actually have one, not from a fixed
    lead source: preferring Gooaye unconditionally published a blank thesis
    whenever its row had none, even though the other tracker had a perfectly
    good one and the unified view serves this row in preference to it.
    """
    _, urls = _combined_evidence(sources)
    rows = list(sources.values())
    stances = {str(row["stance"]) for row in rows}
    # No source labels: this is still a view of the company, and the reader must
    # not have to know which tracker said what. Both texts are kept when both
    # exist — dropping one would lose evidence the fusion would have used.
    theses = [thesis for thesis in (_thesis_of(sources.get(s)) for s in FUSABLE_SOURCES) if thesis]
    return {
        "source": SOURCE_MERGED,
        "symbol": symbol,
        "company_name": _display_name(sources, symbol)[:200],
        "mention_count": sum(int(row.get("mention_count") or 0) for row in rows),
        "stance": (next(iter(stances)) if len(stances) == 1 else "mixed")[:16],
        "confidence": sum(float(row.get("confidence") or 0) for row in rows) / len(rows),
        "thesis": "\n\n".join(dict.fromkeys(theses))[:1200],
        "strengths_json": json.dumps(
            list(dict.fromkeys(p for row in rows for p in _parse(row.get("strengths_json"))))[:20],
            ensure_ascii=False),
        "weaknesses_json": json.dumps(
            list(dict.fromkeys(p for row in rows for p in _parse(row.get("weaknesses_json"))))[:20],
            ensure_ascii=False),
        "evidence_urls_json": json.dumps(urls[:30], ensure_ascii=False),
        "updated_at": now,
    }


def _load_state() -> dict[str, Any]:
    """What the last pass recorded, keyed by symbol.

    A failure here only costs work: an empty map makes every company look
    stale, so the next pass redoes fusions instead of skipping them. That is
    the right direction to fail in, but it is not free, so it is logged loudly.
    """
    try:
        init_db()
        with SessionLocal() as session:
            return {row.symbol: row for row in session.scalars(select(MergedViewState))}
    except Exception as exc:
        logger.warning("合併觀點狀態讀取失敗，這輪全部視為需要重做 | error=%s: %s",
                       type(exc).__name__, exc)
        return {}


def _save_state(written: dict[str, dict[str, dict[str, Any]]], fused: set[str],
                doomed: list[str], now: datetime) -> None:
    try:
        with SessionLocal() as session:
            for symbol, sources in written.items():
                session.merge(MergedViewState(
                    symbol=symbol, fingerprint=_fingerprint(sources),
                    fused=symbol in fused, updated_at=now))
            if doomed:
                session.execute(delete(MergedViewState).where(MergedViewState.symbol.in_(doomed)))
            session.commit()
    except Exception as exc:
        # Losing the record is recoverable — the next pass just redoes the work.
        logger.warning("合併觀點狀態寫入失敗，下輪會重做這些公司 | error=%s: %s",
                       type(exc).__name__, exc)


def _skip(reason: str, **extra: Any) -> dict[str, Any]:
    logger.info("合併觀點略過 | reason=%s", reason)
    return {"merged": 0, "unchanged": 0, "two_sided": 0, "synthesized": 0,
            "not_fused": 0, "removed": 0, "failures": [], "skipped": reason, **extra}


async def rebuild_merged_views(
    selected_symbols: list[str] | None = None,
    *,
    use_llm: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    """Write one `source='merged'` row per company.

    `selected_symbols=None` is a full pass: it also removes merged rows for
    companies neither tracker covers any more. A list restricts the work to
    those companies, removal included — an empty list does nothing at all.

    `use_llm=False` produces the mechanical rollup only. For a two-sided
    company that *replaces* a previously fused thesis with the mechanical text;
    the count comes back as `not_fused` so a caller can see it happened.

    `force=True` also rewrites companies whose merged row is already newer than
    both sources, which is otherwise skipped as unchanged.
    """
    wanted: set[str] | None = None
    if selected_symbols is not None:
        wanted = {value.strip().upper() for value in selected_symbols if value and value.strip()}
        if not wanted:
            return _skip("no symbols requested")

    logger.info("合併觀點開始：讀取來源與既有融合資料…")
    try:
        # Inside the guard, not before it: building the engine parses the DSN
        # and can raise on its own for a malformed XSTOCKTRACKER_DB_URL, which
        # would take down the caller before any of this module's own handling
        # had a chance to turn it into a reported result.
        engine = xstocktracker_engine()
        if engine is None:
            return _skip("XSTOCKTRACKER_DB_URL 未設定")
        with engine.connect() as connection:
            grouped = _load_sources(connection)
            merged_at = _load_merged(connection)
    except Exception as exc:
        # Deliberately broader than SQLAlchemyError: the caller's own sync has
        # already committed, and the promise is that nothing reachable from
        # here can undo that. A driver-level error, a DNS failure or a bug in
        # this module are all the same to the caller — a run that succeeded
        # locally and could not publish. (CancelledError is a BaseException,
        # so shutdown still propagates.)
        logger.warning("合併觀點讀取失敗 | error=%s: %s", type(exc).__name__, exc)
        return _skip(f"讀取 {TABLE} 失敗：{type(exc).__name__}", error=type(exc).__name__)

    live = set(grouped)
    if wanted is not None:
        grouped = {symbol: sources for symbol, sources in grouped.items() if symbol in wanted}
    # Merged rows to drop: companies neither tracker covers any more. Scoped to
    # the requested symbols on a partial run so it never touches a company this
    # run did not look at. Computed from `merged_at` rather than from the work
    # list, because "no sources left at all" is exactly when it matters most —
    # returning early there left the stale merged row served forever.
    doomed = sorted((wanted if wanted is not None else set(merged_at)) - live)

    now = datetime.now(timezone.utc)
    config = merge_config()
    want_fusion = use_llm and bool(config.get("merge_llm_enabled", True))
    state = _load_state()
    pending = {symbol: sources for symbol, sources in grouped.items()
               if force or _is_stale(sources, merged_at.get(symbol), state.get(symbol),
                                     want_fusion=want_fusion)}
    unchanged = len(grouped) - len(pending)
    rows = {symbol: _mechanical_row(symbol, sources, now) for symbol, sources in pending.items()}

    # Only companies both trackers actually cover need fusing.
    two_sided = sorted(symbol for symbol, sources in pending.items() if len(sources) > 1)
    logger.info("待更新 %d 家；未變動跳過 %d 家；單一來源 %d 家；待清理 %d 家",
                len(pending), unchanged, len(pending) - len(two_sided), len(doomed))
    fused, failures = set(), []
    if want_fusion and two_sided:
        fused, failures = await _synthesize(two_sided, pending, rows, config)
    else:
        logger.info("本輪不呼叫融合模型：%s", "沒有待融合的雙來源公司" if not two_sided else "LLM 已停用")
    # A single-source company has nothing to fuse, so its mechanical row is
    # already everything it can be; only two-sided rows can be owed an upgrade.
    fused |= {symbol for symbol, sources in pending.items() if len(sources) < 2}

    try:
        logger.info("開始寫入資料庫：%d 家，清理失效列 %d 家…", len(rows), len(doomed))
        with engine.begin() as connection:
            if rows:
                connection.execute(_UPSERT, list(rows.values()))
            removed = connection.execute(
                _DELETE, {"merged": SOURCE_MERGED, "symbols": doomed}).rowcount if doomed else 0
    except Exception as exc:
        logger.warning("合併觀點寫入失敗 | error=%s: %s", type(exc).__name__, exc)
        return _skip(f"寫入 {TABLE} 失敗：{type(exc).__name__}", error=type(exc).__name__)

    logger.info("融合資料已提交：%d 家；開始記錄本地融合狀態…", len(rows))
    # Only after the shared table actually committed: recording a fusion that
    # was never published would skip it on the next pass too.
    # On a full pass `live` is authoritative, so records for companies that are
    # gone go too — including any that never had a merged row to delete, which
    # would otherwise accumulate forever.
    state_doomed = doomed if wanted is not None else sorted((set(merged_at) | set(state)) - live)
    _save_state(pending, fused, state_doomed, now)

    synthesized = len([symbol for symbol in two_sided if symbol in fused])
    logger.info("合併觀點完成 | written=%d | unchanged=%d | two_sided=%d | synthesized=%d | removed=%d",
                len(rows), unchanged, len(two_sided), synthesized, removed or 0)
    return {
        "merged": len(rows),
        "unchanged": unchanged,
        "two_sided": len(two_sided),
        "synthesized": synthesized,
        "not_fused": len(two_sided) - synthesized,
        "removed": int(removed or 0),
        "failures": failures,
    }


async def _synthesize(
    symbols: list[str],
    grouped: dict[str, dict[str, dict[str, Any]]],
    rows: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> tuple[set[str], list[dict[str, Any]]]:
    """Fuse the two-sided companies in batches, updating `rows` in place.

    Returns the symbols whose row actually holds a fusion — not a count. The
    caller records that per company, so a company the model skipped is retried
    next pass instead of being hidden inside a total that happened to be right.
    """
    batch_size = max(1, int(config.get("merge_batch_size", 2)))
    pause_seconds = max(0.0, float(config.get("merge_pause_seconds", 2.0)))
    max_tokens = max(2048, int(config.get("merge_max_tokens", 8192)))

    completed, failures = set(), []
    total_batches = (len(symbols) + batch_size - 1) // batch_size
    overall_started = time.monotonic()
    logger.info("待融合 %d 家，共 %d 批；批次間隔 %.1f 秒（模型重試另計）",
                len(symbols), total_batches, pause_seconds)

    def report_batch(start: int, batch: list[str], started: float, success: int) -> None:
        processed = min(start + len(batch), len(symbols))
        logger.info(
            "[%d/%d] 本批結束：成功 %d、未融合 %d；已處理 %d/%d 家，累計成功 %d 家；本批 %.1f 秒，累計 %.1f 秒（尚未寫入資料庫）",
            start // batch_size + 1, total_batches, success, len(batch) - success,
            processed, len(symbols), len(completed), time.monotonic() - started,
            time.monotonic() - overall_started)

    starts = range(0, len(symbols), batch_size)
    async for start in paced(starts, pause_seconds):
        batch = symbols[start:start + batch_size]
        batch_started = time.monotonic()
        logger.info("[%d/%d] 開始：%s", start // batch_size + 1, total_batches, "、".join(batch))
        blocks = {}
        for symbol in batch:
            evidence, _ = _combined_evidence(grouped[symbol])
            if evidence:
                blocks[symbol] = f"COMPANY: {rows[symbol]['company_name']}\n{evidence}"
        if not blocks:
            report_batch(start, batch, batch_started, 0)
            continue
        try:
            results = await synthesize_merged_views(blocks, max_tokens=max_tokens)
        except Exception as exc:
            # The mechanical row is already in `rows`, so the company stays
            # readable; only the fused narrative is missing.
            failures.append({"symbols": list(blocks), "error": type(exc).__name__})
            logger.warning("合併觀點分析失敗，保留機械彙整 | symbols=%s | error=%s: %s",
                           ",".join(batch), type(exc).__name__, exc)
            report_batch(start, batch, batch_started, 0)
            continue
        applied = set()
        for symbol, item in results.items():
            if symbol not in rows or not item["thesis"]:
                continue  # an empty answer is worse than the mechanical text
            leak = source_leak(item["thesis"])
            if leak:
                # Kept rather than discarded: the fusion is still the better
                # text, and throwing it away would cost another call to get
                # something no more likely to comply. Logged so it is findable.
                logger.warning("融合論點提及了來源，違反 prompt 規則 | symbol=%s | 出現=%r",
                               symbol, leak)
            rows[symbol].update({
                "stance": item["stance"][:16],
                "thesis": item["thesis"][:1200],
                "strengths_json": _json_list(json.dumps(item["strengths"], ensure_ascii=False)),
                "weaknesses_json": _json_list(json.dumps(item["weaknesses"], ensure_ascii=False)),
            })
            applied.add(symbol)
        completed |= applied
        # Anything asked for and not applied is a failure, whether the model
        # omitted the company or answered with an empty thesis. Comparing
        # against `results` alone reported an empty answer as a success.
        missing = sorted(set(blocks) - applied)
        if missing:
            failures.append({"symbols": missing, "error": "MissingCompanyResults"})
            logger.warning("本批未融合，保留機械彙整：%s", "、".join(missing))
        report_batch(start, batch, batch_started, len(applied))
    return completed, failures
