"""Orchestrates one Gooaye (股癌) sync run:

1. Pull the whatmkreallysaid.com bulk transcript pack (primary source, ~all
   history) and the SoundOn RSS feed (source of truth for "which episodes
   exist" and their audio URLs).
2. For episodes the RSS feed knows about but the pack hasn't caught up to yet,
   optionally fill the gap with a local Whisper transcription of the audio.
3. Once a pack (or vocus.cc free) transcript later becomes available for an
   episode that was Whisper-filled, compute a similarity score between the two
   as a lightweight "audio vs. transcript" comparison record.
4. Split each new/changed episode's transcript into chunks (chunking.py), run
   them through the local LLM (llm.py), merge the per-chunk results into one
   episode-level analysis, resolve tickers against the alias list, and persist
   everything to Postgres.
5. Roll episode-level tickers up into company_insights.
"""
from __future__ import annotations

import difflib
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Episode
from app.runtime_config import load_runtime_config
from app.services import feed as feed_service
from app.services import transcripts_pack
from app.services import vocus_free
from app.services import whisper_transcribe
from app.services.chunking import split_transcript
from app.services.company_insights import rebuild_company_insights
from app.services.entity_resolver import canonicalize_symbols
from app.services.llm import analyze_chunks

logger = logging.getLogger("uvicorn.error")


async def sync_gooaye(limit: int | None = None) -> dict[str, Any]:
    config = load_runtime_config()
    sources_cfg = config.get("sources", {})
    sync_cfg = config.get("sync", {})
    pack_cfg = sources_cfg.get("transcripts_pack", {})
    feed_cfg = sources_cfg.get("feed", {})
    vocus_cfg = sources_cfg.get("vocus", {})
    whisper_cfg = sources_cfg.get("whisper", {})
    settings = get_settings()

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        pack_episodes = await transcripts_pack.fetch_transcript_pack(
            pack_cfg["pack_url"], pack_cfg["episode_url_template"], client,
        )
        feed_episodes = await feed_service.fetch_feed_episodes(feed_cfg["rss_url"], client)
        vocus_episodes: list[dict[str, Any]] = []
        if vocus_cfg.get("enabled", False):
            vocus_episodes = await vocus_free.list_free_articles(vocus_cfg["room_url"], client)

        pack_by_number = {e["episode_number"]: e for e in pack_episodes}
        feed_by_number = {e["episode_number"]: e for e in feed_episodes}
        vocus_by_number = {e["episode_number"]: e for e in vocus_episodes}

        with SessionLocal() as session:
            existing_rows = {row.episode_number: row for row in session.scalars(select(Episode))}

        whisper_texts: dict[int, str] = {}
        if whisper_cfg.get("enabled", True):
            gap_numbers = sorted(set(feed_by_number) - set(pack_by_number) - set(vocus_by_number))
            gap_limit = int(whisper_cfg.get("gap_episode_limit", 3))
            for number in gap_numbers[-gap_limit:]:
                row = existing_rows.get(number)
                if row is not None and row.transcript_source == "whisper" and row.transcript_text:
                    continue  # already filled; waiting for the written transcript to catch up
                audio_url = feed_by_number[number].get("audio_url")
                if not audio_url:
                    continue
                try:
                    whisper_texts[number] = await whisper_transcribe.transcribe_episode_audio(
                        audio_url, settings.whisper_model, settings.whisper_device, client,
                    )
                except Exception as exc:
                    logger.exception("Gooaye Whisper 轉譯失敗 | episode=%s | error=%s: %s", number, type(exc).__name__, exc)

        all_numbers = sorted(set(pack_by_number) | set(feed_by_number) | set(vocus_by_number) | set(whisper_texts))
        analysis_limit = limit if limit is not None else int(sync_cfg.get("incremental_new_limit", 20))

        stored = 0
        analyzed = 0
        affected_symbols: set[str] = set()
        pending_analysis: list[int] = []

        with SessionLocal() as session:
            for number in all_numbers:
                pack = pack_by_number.get(number)
                feed_item = feed_by_number.get(number)
                vocus_item = vocus_by_number.get(number)
                whisper_text = whisper_texts.get(number)
                row = session.scalar(select(Episode).where(Episode.episode_number == number))
                is_new = row is None
                if row is None:
                    row = Episode(episode_number=number, published_at=datetime.now(timezone.utc))
                    session.add(row)

                _apply_metadata(row, pack, feed_item, vocus_item)
                content_changed = _apply_transcript(row, pack, vocus_item, whisper_text)

                row.updated_at = datetime.now(timezone.utc)
                stored += int(is_new or content_changed)
                if (is_new or content_changed or row.analyzed_at is None) and row.transcript_text:
                    pending_analysis.append(number)
            session.commit()

        # Analyze the newest pending episodes first, capped per run.
        pending_analysis.sort(reverse=True)
        for number in pending_analysis[:max(0, analysis_limit)]:
            with SessionLocal() as session:
                row = session.scalar(select(Episode).where(Episode.episode_number == number))
                if row is None or not row.transcript_text:
                    continue
                symbols = await _analyze_episode(row, sync_cfg)
                affected_symbols.update(symbols)
                analyzed += 1
                session.commit()

        insights_result: dict[str, Any] = {"skipped": "no affected symbols"}
        if affected_symbols:
            insights_result = await rebuild_company_insights(selected_symbols=sorted(affected_symbols))

        return {
            "pack_episodes": len(pack_episodes),
            "feed_episodes": len(feed_episodes),
            "vocus_free_episodes": len(vocus_episodes),
            "whisper_filled": len(whisper_texts),
            "episodes_stored_or_updated": stored,
            "episodes_analyzed": analyzed,
            "pending_analysis_backlog": max(0, len(pending_analysis) - analyzed),
            "affected_symbols": sorted(affected_symbols),
            "company_insights": insights_result,
        }


def _apply_metadata(row: Episode, pack: dict | None, feed_item: dict | None, vocus_item: dict | None) -> None:
    source = pack or feed_item or vocus_item
    if source is None:
        return
    row.title = str(source.get("title") or row.title or "")
    row.published_at = source.get("published_at") or row.published_at
    if pack is not None:
        row.description = pack.get("description", row.description)
        row.source_url = pack.get("source_url", row.source_url)
    if feed_item is not None and feed_item.get("audio_url"):
        row.audio_url = feed_item["audio_url"]
    if vocus_item is not None:
        row.vocus_url = vocus_item.get("vocus_url", row.vocus_url)


def _apply_transcript(row: Episode, pack: dict | None, vocus_item: dict | None, whisper_text: str | None) -> bool:
    """Pick the best available transcript for this sync pass. Returns True if
    the authoritative transcript_text changed (i.e. re-analysis is needed)."""
    previous_text = row.transcript_text
    written_text = (pack or {}).get("transcript_text") or (vocus_item or {}).get("transcript_text")
    written_source = "whatmkreallysaid" if pack else ("vocus_free" if vocus_item else None)

    if written_text:
        if row.transcript_source == "whisper" and row.whisper_transcript_text:
            score = difflib.SequenceMatcher(None, row.whisper_transcript_text, written_text).ratio()
            row.comparison_score = round(score, 4)
            row.comparison_notes = (
                f"本機 Whisper 逐字稿與 {written_source} 正式逐字稿的相似度為 {score:.1%}"
                "（正式逐字稿到位後已改用正式版本）。"
            )
        row.transcript_text = written_text
        row.transcript_source = written_source
    elif whisper_text:
        row.transcript_text = whisper_text
        row.transcript_source = "whisper"
        row.whisper_transcript_text = whisper_text
    # else: keep whatever was already stored (nothing new this run).

    return row.transcript_text != previous_text


async def _analyze_episode(row: Episode, sync_cfg: dict[str, Any]) -> set[str]:
    chunk_max = int(sync_cfg.get("chunk_max_characters", 11000))
    chunks = split_transcript(row.transcript_text, max_characters=chunk_max)
    if not chunks:
        return set()
    try:
        analyses = await analyze_chunks(chunks)
    except Exception as exc:
        logger.exception("Gooaye 分析失敗 | episode=%s | error=%s: %s", row.episode_number, type(exc).__name__, exc)
        return set()

    sentiments = Counter(item["sentiment"] for item in analyses)
    stance = max(("bullish", "bearish", "neutral"), key=lambda key: sentiments[key])
    tickers = {value.upper() for item in analyses for value in item["tickers"]}
    tickers = canonicalize_symbols(tickers)
    sectors = list(dict.fromkeys(value for item in analyses for value in item["sectors"]))
    summaries = [item["summary_zh_tw"] for item in analyses if item["summary_zh_tw"]]
    reasons = [item["reason"] for item in analyses if item["reason"]]

    row.sentiment = stance
    row.importance = max((item["importance"] for item in analyses), default=0.5)
    row.sectors = ",".join(sectors)
    row.tickers = ",".join(sorted(tickers))
    row.summary = "\n\n".join(summaries) or row.summary
    row.analysis_reason = "；".join(reasons) or row.analysis_reason
    row.analyzed_at = datetime.now(timezone.utc)
    return tickers
