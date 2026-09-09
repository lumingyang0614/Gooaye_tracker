"""Roll up per-segment ticker mentions into one row per company.

Two stages, deliberately split by what each is good at:

1. A mechanical pass over EpisodeSegment rows (one per transcript chunk, see
   app/services/sync.py) decides stance, confidence, mention count, market and
   evidence links. Using segments rather than the episode-level
   sentiment/tickers columns means a stock discussed bearishly in one part of
   an episode isn't misclassified as bullish just because the rest of that
   episode leaned bullish. It reads *every* segment, is reproducible, and
   costs nothing.

2. A company-level LLM pass (app/services/llm.py's analyze_companies) writes
   the prose: a thesis plus the strengths and weaknesses the host actually
   gave. Before this existed, thesis was three segment summaries pasted
   together and strengths/weaknesses were hard-coded empty lists — the company
   view had no argument in it, only mentions.

Stage 2 never overwrites stage 1's stance/confidence: it only sees the top
handful of segments, while the vote sees all of them. A failed or skipped LLM
call therefore degrades to the old mechanical thesis rather than a blank card,
and the rows stay usable.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.db import SessionLocal
from app.models import CompanyInsight, EpisodeSegment
from app.runtime_config import load_runtime_config
from app.services.entity_resolver import canonicalize_symbol_map, resolve_market
from app.services.llm import analyze_companies
from app.services.taiwan_securities import load_taiwan_security_lookup

logger = logging.getLogger("uvicorn.error")


def _company_names() -> dict[str, str]:
    """Canonical symbol -> display name.

    The TWSE/TPEx master supplies the bulk of the names and the hand-curated
    seed list overrides it, so a company the exchanges know about shows up as
    「文曄」 rather than as a bare 3036.TW once the master has been synced.
    """
    names = {
        str(item.get("symbol", "")).strip().upper(): str(item.get("company_name", "")).strip()
        for item in load_taiwan_security_lookup().values()
        if item.get("symbol") and item.get("company_name")
    }
    names.update({
        str(company.get("symbol", "")).strip().upper(): str(company.get("company_name", "")).strip()
        for company in load_runtime_config().get("company_aliases", [])
        if company.get("symbol") and company.get("company_name")
    })
    return names


def _segments_by_symbol() -> dict[str, list[EpisodeSegment]]:
    """Load every ticker-tagged segment (eagerly joined with its parent
    episode) and group it by the canonical symbols it mentions. Loaded once
    per call so both rebuild_company_insights and list_symbol_segments read
    a consistent snapshot.

    Grouping resolves each stored ticker through the alias list and security
    masters again rather than trusting the string that was written at analysis
    time. Segments analyzed before `scripts.sync_taiwan_securities` ran — or
    before an alias was added to config/settings.yaml — hold the raw name the
    model produced ("文曄"), and grouping on that would keep them in a company
    of their own forever, never merging with the 3036.TW rows that later
    mentions produce. This is what makes rebuilding after a master or alias
    update actually change anything.
    """
    with SessionLocal() as session:
        segments = list(
            session.scalars(
                select(EpisodeSegment)
                .where(EpisodeSegment.tickers != "")
                .options(joinedload(EpisodeSegment.episode))
            )
        )
    raw_symbols = {
        value.strip()
        for segment in segments
        for value in segment.tickers.split(",")
        if value.strip()
    }
    canonical = canonicalize_symbol_map(raw_symbols)
    grouped: dict[str, list[EpisodeSegment]] = defaultdict(list)
    for segment in segments:
        symbols = {
            canonical.get(value.strip(), value.strip().upper())
            for value in segment.tickers.split(",")
            if value.strip()
        }
        for symbol in symbols:
            grouped[symbol].append(segment)
    return grouped


def _partial_rebuild_symbols(selected_symbols: list[str], existing_symbols: set[str]) -> list[str]:
    """Expand a caller's symbol list to everything that shares its identity.

    A partial rebuild that visited only the symbols it was handed would leave
    the same company on screen twice after the security master learns a name:
    segments regroup under 3036.TW, a new row appears, and the 「文曄」 row
    written before the master existed is never looked at again. Only a full
    rebuild cleaned that up, and the sync path never runs one.

    So the set is widened at both ends — each requested symbol is resolved to
    its canonical form (the company that actually needs rebuilding), and every
    stored row that now resolves into one of those is pulled in too (the old
    names, which have no segments of their own any more and get deleted by the
    caller's normal stale-row branch).
    """
    requested = {symbol.strip().upper() for symbol in selected_symbols if symbol.strip()}
    if not requested:
        return []
    canonical = canonicalize_symbol_map(requested | existing_symbols)
    targets = {canonical.get(symbol, symbol) for symbol in requested}
    superseded_rows = {
        stored
        for stored, resolved in canonical.items()
        if stored in existing_symbols and resolved in targets and resolved != stored
    }
    return sorted(targets | requested | superseded_rows)


async def rebuild_company_insights(
    selected_symbols: list[str] | None = None,
    *,
    use_llm: bool = False,
) -> dict[str, Any]:
    """Rebuild company rows from the stored segments.

    The narrative stage is opt-in (`use_llm=True`) rather than on by default so
    that the default call does no network I/O at all: the mechanical rollup is
    also what migrations and tests want, and silently making model calls from a
    plain rebuild would make both slow and non-deterministic. Callers that do
    want narratives — the sync run, the CLI script, the API endpoint — ask for
    them explicitly, and `analysis.company_analysis_enabled` can still switch
    the stage off globally.
    """
    config = load_runtime_config().get("analysis", {})
    use_llm = use_llm and bool(config.get("company_analysis_enabled", True))

    grouped = _segments_by_symbol()
    names = _company_names()
    updated = 0
    removed = 0

    with SessionLocal() as session:
        existing_symbols = set(session.scalars(select(CompanyInsight.symbol)))
        if selected_symbols is None:
            # A full rebuild also has to catch symbols that still have a
            # CompanyInsight row but no longer have any segments at all
            # (e.g. their only mention got removed on re-analysis in a
            # sync run that only passed the newly-affected symbols along).
            symbols = sorted(set(grouped.keys()) | existing_symbols)
        else:
            symbols = _partial_rebuild_symbols(selected_symbols, existing_symbols)

        live_symbols: list[str] = []
        for symbol in symbols:
            segments_for_symbol = grouped.get(symbol, [])
            row = session.scalar(select(CompanyInsight).where(CompanyInsight.symbol == symbol))
            if not segments_for_symbol:
                # This symbol is no longer mentioned anywhere (e.g. it was
                # dropped on re-analysis) — its old insight is now stale.
                if row is not None:
                    session.delete(row)
                    removed += 1
                continue
            if row is None:
                row = CompanyInsight(symbol=symbol)
                session.add(row)
            _apply_insight(row, symbol, segments_for_symbol, names)
            live_symbols.append(symbol)
            updated += 1
        # Commit the mechanical baseline before spending anything on the LLM,
        # so a model failure mid-run still leaves every row readable.
        session.commit()

    llm_result = {"attempted": 0, "completed": 0}
    if use_llm and live_symbols:
        llm_result = await _write_company_narratives(live_symbols, grouped, config)

    return {
        "symbols_considered": len(symbols),
        "symbols_updated": updated,
        "symbols_removed": removed,
        "narratives_attempted": llm_result["attempted"],
        "narratives_completed": llm_result["completed"],
    }


def _apply_insight(row: CompanyInsight, symbol: str, segments: list[EpisodeSegment], names: dict[str, str]) -> None:
    """Mechanical stage: everything derivable from the segments themselves."""
    stances = Counter(segment.sentiment for segment in segments)
    stance = max(("bullish", "bearish", "neutral"), key=lambda key: stances[key])
    top = _top_segments(segments, limit=3)
    mentioned_episodes = {segment.episode.episode_number for segment in segments}

    row.company_name = names.get(symbol, symbol)
    row.market = resolve_market(symbol)
    row.mention_count = len(mentioned_episodes)
    row.stance = stance
    # Mean segment importance. Note this is *not* a confidence in the stance —
    # the UI labels it 重要度 for that reason. Stance agreement (how lopsided
    # `stances` is) would be the honest confidence measure, but it needs its
    # own column: with a single low-importance mention agreement reads 100%,
    # which overstates a passing remark far worse than importance does.
    row.confidence = sum(segment.importance for segment in segments) / len(segments)
    # Fallback prose, replaced by _write_company_narratives when the LLM runs.
    row.thesis = "\n\n".join(
        f"EP{segment.episode.episode_number}｜{segment.summary}"[:400] for segment in top if segment.summary
    )
    row.strengths_json = json.dumps([], ensure_ascii=False)
    row.weaknesses_json = json.dumps([], ensure_ascii=False)
    row.evidence_urls_json = json.dumps(
        list(dict.fromkeys(segment.episode.source_url for segment in top if segment.episode.source_url)),
        ensure_ascii=False,
    )
    row.updated_at = datetime.now(timezone.utc)


def _top_segments(segments: list[EpisodeSegment], limit: int) -> list[EpisodeSegment]:
    """Most important segments first, newest episode breaking ties, so a
    company's evidence leans on recent commentary rather than whichever old
    episode happened to score highest."""
    return sorted(
        segments,
        key=lambda segment: (segment.importance, segment.episode.episode_number),
        reverse=True,
    )[:limit]


def _evidence_block(segments: list[EpisodeSegment], segment_limit: int, characters: int) -> str:
    stances = Counter(segment.sentiment for segment in segments)
    episodes = {segment.episode.episode_number for segment in segments}
    lines = [
        f"MENTIONED IN: {len(episodes)} 集 / {len(segments)} 個片段",
        f"SEGMENT STANCES: bullish {stances['bullish']} / bearish {stances['bearish']} / neutral {stances['neutral']}",
        "SEGMENTS:",
    ]
    for segment in _top_segments(segments, segment_limit):
        summary = (segment.summary or "").strip()[:characters]
        if not summary:
            continue
        lines.append(
            f"[EP{segment.episode.episode_number} · {segment.sentiment} · 重要度 {segment.importance:.2f}] {summary}"
        )
    return "\n".join(lines)


async def _write_company_narratives(
    symbols: list[str],
    grouped: dict[str, list[EpisodeSegment]],
    config: dict[str, Any],
) -> dict[str, int]:
    """Stage 2: ask the LLM for a thesis and strengths/weaknesses per company.

    Batched to keep the number of round trips proportional to companies rather
    than to episodes. Each batch is committed on its own so a later failure
    doesn't discard narratives already written.
    """
    batch_size = max(1, int(config.get("company_analysis_batch_size", 2)))
    segment_limit = max(1, int(config.get("company_analysis_segment_limit", 8)))
    characters = max(200, int(config.get("company_analysis_segment_characters", 600)))
    max_tokens = max(2048, int(config.get("company_analysis_max_tokens", 8192)))

    names = _company_names()
    attempted = 0
    completed = 0
    for start in range(0, len(symbols), batch_size):
        batch = symbols[start:start + batch_size]
        blocks = {
            symbol: f"COMPANY: {names.get(symbol, symbol)}\n"
                    + _evidence_block(grouped[symbol], segment_limit, characters)
            for symbol in batch
            if grouped.get(symbol)
        }
        if not blocks:
            continue
        attempted += len(blocks)
        try:
            results = await analyze_companies(blocks, max_tokens=max_tokens)
        except Exception as exc:
            logger.exception(
                "Gooaye 公司觀點分析失敗，保留逐段彙整結果 | symbols=%s | error=%s: %s",
                ",".join(batch), type(exc).__name__, exc,
            )
            continue

        with SessionLocal() as session:
            for symbol, item in results.items():
                row = session.scalar(select(CompanyInsight).where(CompanyInsight.symbol == symbol))
                if row is None:
                    continue
                # An empty thesis is worse than the mechanical fallback that is
                # already stored, so only overwrite when the model said something.
                if item["thesis"]:
                    row.thesis = item["thesis"][:1200]
                row.strengths_json = json.dumps(item["strengths"], ensure_ascii=False)
                row.weaknesses_json = json.dumps(item["weaknesses"], ensure_ascii=False)
                row.updated_at = datetime.now(timezone.utc)
                completed += 1
            session.commit()

    if attempted and completed < attempted:
        logger.warning("部分公司未取得模型論點，維持逐段彙整結果 | attempted=%s | completed=%s", attempted, completed)
    return {"attempted": attempted, "completed": completed}


def list_symbol_segments(symbol: str) -> list[dict[str, Any]]:
    """All segments that mention `symbol`, newest episode first, for a
    per-stock drill-down (e.g. "EP482 幾分鐘討論台積電是看空的，儘管整集偏多")."""
    target = symbol.strip().upper()
    segments = _segments_by_symbol().get(target, [])
    ordered = sorted(segments, key=lambda segment: (segment.episode.episode_number, segment.chunk_index), reverse=True)
    return [
        {
            "episode_number": segment.episode.episode_number,
            "episode_title": segment.episode.title,
            "chunk_index": segment.chunk_index,
            "sentiment": segment.sentiment,
            "importance": segment.importance,
            "summary": segment.summary,
            "reason": segment.reason,
            "source_url": segment.episode.source_url,
            "published_at": segment.episode.published_at.isoformat() if segment.episode.published_at else None,
        }
        for segment in ordered
    ]


def list_company_insights() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.scalars(select(CompanyInsight).order_by(CompanyInsight.mention_count.desc())).all()
    return [
        {
            "symbol": row.symbol,
            "company_name": row.company_name,
            "market": row.market or "UNKNOWN",
            "mention_count": row.mention_count,
            "stance": row.stance,
            "confidence": row.confidence,
            "thesis": row.thesis,
            "strengths": json.loads(row.strengths_json or "[]"),
            "weaknesses": json.loads(row.weaknesses_json or "[]"),
            "evidence_urls": json.loads(row.evidence_urls_json or "[]"),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in rows
    ]
