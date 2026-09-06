"""Roll up per-episode ticker mentions into one row per company.

Simpler than Serenity Stock Tracker's equivalent (no second LLM pass to write
a bespoke thesis/strengths/weaknesses per company) — thesis here is just the
highest-importance episode summaries that mentioned the symbol. This keeps
each sync run to a single LLM call per episode; a richer per-company writeup
could be added later the same way Serenity's company_analysis.py does it.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import CompanyInsight, Episode
from app.runtime_config import load_runtime_config


def _company_names() -> dict[str, str]:
    return {
        str(company.get("symbol", "")).strip().upper(): str(company.get("company_name", "")).strip()
        for company in load_runtime_config().get("company_aliases", [])
        if company.get("symbol") and company.get("company_name")
    }


async def rebuild_company_insights(selected_symbols: list[str] | None = None) -> dict[str, int]:
    with SessionLocal() as session:
        episodes = list(session.scalars(select(Episode).where(Episode.tickers != "")))

    grouped: dict[str, list[Episode]] = defaultdict(list)
    for episode in episodes:
        for symbol in {value.strip().upper() for value in episode.tickers.split(",") if value.strip()}:
            grouped[symbol].append(episode)

    symbols = selected_symbols or list(grouped.keys())
    names = _company_names()
    updated = 0
    with SessionLocal() as session:
        for symbol in symbols:
            episodes_for_symbol = grouped.get(symbol, [])
            if not episodes_for_symbol:
                continue
            row = session.scalar(select(CompanyInsight).where(CompanyInsight.symbol == symbol))
            if row is None:
                row = CompanyInsight(symbol=symbol)
                session.add(row)
            _apply_insight(row, symbol, episodes_for_symbol, names)
            updated += 1
        session.commit()
    return {"symbols_considered": len(symbols), "symbols_updated": updated}


def _apply_insight(row: CompanyInsight, symbol: str, episodes: list[Episode], names: dict[str, str]) -> None:
    stances = Counter(episode.sentiment for episode in episodes)
    stance = max(("bullish", "bearish", "neutral"), key=lambda key: stances[key])
    top = sorted(episodes, key=lambda episode: episode.importance, reverse=True)[:3]

    row.company_name = names.get(symbol, symbol)
    row.mention_count = len(episodes)
    row.stance = stance
    row.confidence = sum(episode.importance for episode in episodes) / len(episodes)
    row.thesis = "\n\n".join(f"EP{episode.episode_number}｜{episode.summary}"[:400] for episode in top if episode.summary)
    row.strengths_json = json.dumps([], ensure_ascii=False)
    row.weaknesses_json = json.dumps([], ensure_ascii=False)
    row.evidence_urls_json = json.dumps([episode.source_url for episode in top if episode.source_url], ensure_ascii=False)
    row.updated_at = datetime.now(timezone.utc)


def list_company_insights() -> list[dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.scalars(select(CompanyInsight).order_by(CompanyInsight.mention_count.desc())).all()
    return [
        {
            "symbol": row.symbol,
            "company_name": row.company_name,
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
