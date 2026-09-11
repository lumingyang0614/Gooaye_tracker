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

Deliberately *not* done here: any re-weighting of the two sources. Both inputs
are already aggregates — X Stock Tracker applies its own source weighting
(the Serenity playbook bonus in its `analysis.priority_weight`) when it builds
its row, and Gooaye weights by segment importance when it builds its own.
Re-weighting them again here would double-count decisions both projects have
already made, so each arrives as one considered view and the model integrates
them on their merits.

Cost is kept proportional to the actual work: only companies that really have
two views cost a model call. A company just one tracker covers is copied
through mechanically, because there is nothing to fuse.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.runtime_config import load_runtime_config
from app.services.llm import paced, synthesize_merged_views
from app.services.securities import xstocktracker_engine
from app.services.xstock_push import SOURCE_GOOAYE, TABLE, _json_list

logger = logging.getLogger("uvicorn.error")

SOURCE_MERGED = "merged"
SOURCE_X = "x"

# Human-readable labels for the evidence block. The prompt forbids echoing
# these back, but the model still needs to know which view is which to notice
# that they disagree.
_LABELS = {SOURCE_GOOAYE: "《股癌》Podcast 逐字稿分析", SOURCE_X: "X 分析師社群彙總"}

_SELECT = text(f"""
    SELECT source, upper(btrim(symbol)) AS symbol, company_name, stance, confidence,
           thesis, strengths_json, weaknesses_json, evidence_urls_json, mention_count
    FROM {TABLE}
    WHERE source <> :merged
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


def _load_sources() -> dict[str, dict[str, dict[str, Any]]]:
    """symbol -> {source -> row}, for every company either tracker covers."""
    engine = xstocktracker_engine()
    if engine is None:
        return {}
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    with engine.connect() as connection:
        for row in connection.execute(_SELECT, {"merged": SOURCE_MERGED}):
            data = dict(row._mapping)
            symbol = data["symbol"]
            if symbol:
                grouped.setdefault(symbol, {})[data["source"]] = data
    return grouped


def _display_name(sources: dict[str, dict[str, Any]], symbol: str) -> str:
    """Prefer a name that is not just the ticker repeated, Gooaye's first."""
    for source in (SOURCE_GOOAYE, SOURCE_X):
        name = str(sources.get(source, {}).get("company_name") or "").strip()
        if name and name.upper() != symbol:
            return name
    return symbol


def _combined_evidence(sources: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """The prompt block for one company, plus its merged evidence links."""
    blocks, urls = [], []
    for source in (SOURCE_GOOAYE, SOURCE_X):
        row = sources.get(source)
        if not row:
            continue
        lines = [f"[{_LABELS[source]}]", f"目前立場：{row['stance']}"]
        thesis = str(row.get("thesis") or "").strip()
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
    """
    _, urls = _combined_evidence(sources)
    rows = list(sources.values())
    stances = {str(row["stance"]) for row in rows}
    lead = sources.get(SOURCE_GOOAYE) or rows[0]
    return {
        "source": SOURCE_MERGED,
        "symbol": symbol,
        "company_name": _display_name(sources, symbol)[:200],
        "mention_count": sum(int(row.get("mention_count") or 0) for row in rows),
        "stance": (next(iter(stances)) if len(stances) == 1 else "mixed")[:16],
        "confidence": sum(float(row.get("confidence") or 0) for row in rows) / len(rows),
        "thesis": str(lead.get("thesis") or "").strip(),
        "strengths_json": json.dumps(
            list(dict.fromkeys(p for row in rows for p in _parse(row.get("strengths_json"))))[:20],
            ensure_ascii=False),
        "weaknesses_json": json.dumps(
            list(dict.fromkeys(p for row in rows for p in _parse(row.get("weaknesses_json"))))[:20],
            ensure_ascii=False),
        "evidence_urls_json": json.dumps(urls[:30], ensure_ascii=False),
        "updated_at": now,
    }


async def rebuild_merged_views(
    selected_symbols: list[str] | None = None,
    *,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Write one `source='merged'` row per company.

    `use_llm=False` produces the mechanical rollup only — useful to populate
    every company cheaply, or to refresh after a push without paying for
    synthesis again.
    """
    engine = xstocktracker_engine()
    if engine is None:
        logger.info("合併觀點略過 | reason=XSTOCKTRACKER_DB_URL 未設定")
        return {"merged": 0, "synthesized": 0, "skipped": "XSTOCKTRACKER_DB_URL 未設定"}

    grouped = _load_sources()
    if selected_symbols is not None:
        wanted = {value.strip().upper() for value in selected_symbols if value.strip()}
        grouped = {symbol: sources for symbol, sources in grouped.items() if symbol in wanted}
    if not grouped:
        logger.info("合併觀點略過 | reason=no companies")
        return {"merged": 0, "synthesized": 0, "skipped": "no companies"}

    now = datetime.now(timezone.utc)
    rows = {symbol: _mechanical_row(symbol, sources, now) for symbol, sources in grouped.items()}

    # Only companies both trackers actually cover need fusing.
    two_sided = sorted(symbol for symbol, sources in grouped.items() if len(sources) > 1)
    config = merge_config()
    synthesized, failures = 0, []
    if use_llm and two_sided and config.get("merge_llm_enabled", True):
        synthesized, failures = await _synthesize(two_sided, grouped, rows, config)

    with engine.begin() as connection:
        connection.execute(_UPSERT, list(rows.values()))
        # A merged row whose company disappeared from both trackers is stale.
        removed = connection.execute(text(
            f"DELETE FROM {TABLE} WHERE source = :merged AND NOT (symbol = ANY(:live))"
        ), {"merged": SOURCE_MERGED, "live": sorted(_load_sources())}).rowcount if selected_symbols is None else 0

    logger.info("合併觀點完成 | companies=%d | two_sided=%d | synthesized=%d | removed=%d",
                len(rows), len(two_sided), synthesized, removed or 0)
    return {
        "merged": len(rows),
        "two_sided": len(two_sided),
        "synthesized": synthesized,
        "removed": int(removed or 0),
        "failures": failures,
    }


async def _synthesize(
    symbols: list[str],
    grouped: dict[str, dict[str, dict[str, Any]]],
    rows: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> tuple[int, list[dict[str, Any]]]:
    """Fuse the two-sided companies in batches, updating `rows` in place."""
    batch_size = max(1, int(config.get("merge_batch_size", 2)))
    pause_seconds = max(0.0, float(config.get("merge_pause_seconds", 2.0)))
    max_tokens = max(2048, int(config.get("merge_max_tokens", 8192)))

    completed, failures = 0, []
    starts = range(0, len(symbols), batch_size)
    async for start in paced(starts, pause_seconds):
        batch = symbols[start:start + batch_size]
        blocks = {}
        for symbol in batch:
            evidence, _ = _combined_evidence(grouped[symbol])
            if evidence:
                blocks[symbol] = f"COMPANY: {rows[symbol]['company_name']}\n{evidence}"
        if not blocks:
            continue
        try:
            results = await synthesize_merged_views(blocks, max_tokens=max_tokens)
        except Exception as exc:
            # The mechanical row is already in `rows`, so the company stays
            # readable; only the fused narrative is missing.
            failures.append({"symbols": list(blocks), "error": type(exc).__name__})
            logger.warning("合併觀點分析失敗，保留機械彙整 | symbols=%s | error=%s: %s",
                           ",".join(batch), type(exc).__name__, exc)
            continue
        for symbol, item in results.items():
            if not item["thesis"]:
                continue  # an empty answer is worse than the mechanical text
            rows[symbol].update({
                "stance": item["stance"][:16],
                "thesis": item["thesis"][:1200],
                "strengths_json": _json_list(json.dumps(item["strengths"], ensure_ascii=False)),
                "weaknesses_json": _json_list(json.dumps(item["weaknesses"], ensure_ascii=False)),
            })
            completed += 1
        missing = sorted(set(blocks) - set(results))
        if missing:
            failures.append({"symbols": missing, "error": "MissingCompanyResults"})
    return completed, failures
