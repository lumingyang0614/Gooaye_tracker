"""Push Gooaye's company insights into the X Stock Tracker's x_company_views.

X Stock Tracker (E:\\X_stock_tracker) aggregates one row per company from its
own source — Alva @zet 的 X 貼文 — and Gooaye aggregates one row per company
from 股癌 逐字稿. They answer the same question about the same companies from
different evidence, so this writes Gooaye's rows into that project's table
where its dashboard already reads them.

The two trackers' rows sit side by side rather than merged: x_company_views
carries a `source` column ('x' / 'gooaye') and a UNIQUE (source, symbol) key,
so 2330.TW appears once per source with each tracker's own stance, thesis and
evidence links intact. That column is also what keeps the two from destroying
each other — X's rebuild_company_views deletes only source='x' rows, and this
module only ever writes and deletes source='gooaye' ones. Neither can see or
touch the other's data.

Written with SQL Core against the other project's table rather than by
importing its SQLAlchemy models, deliberately: Gooaye must not take a code
dependency on X Stock Tracker (the two are separate projects with separate
virtualenvs), and the shape written here is exactly the contract documented in
both READMEs.

Every failure degrades instead of raising. A sync run that produced good
episode analysis must not be reported as failed because a second, optional
database was unreachable or had not been migrated yet — the insights are
already safely in Gooaye's own database, and the next push retries.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db import SessionLocal
from app.models import CompanyInsight
from app.runtime_config import load_runtime_config
from app.services.securities import xstocktracker_engine

logger = logging.getLogger("uvicorn.error")

# The value written into x_company_views.source for every row this module
# writes. X Stock Tracker uses 'x' for its own; the two must never collide.
SOURCE_GOOAYE = "gooaye"

TABLE = "x_company_views"

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

# Column widths in the target table. Gooaye's own columns are the same width,
# so these only bite on data that arrived before a width changed on one side;
# truncating loses a little text, while overflowing loses the whole push.
_SYMBOL_MAX = 32
_COMPANY_NAME_MAX = 200
_STANCE_MAX = 16


def push_config() -> dict[str, Any]:
    return load_runtime_config().get("xstocktracker", {}) or {}


def is_enabled() -> bool:
    """True when the push is switched on *and* a target database is configured."""
    return bool(push_config().get("push_company_insights", True)) and xstocktracker_engine() is not None


def _skip(reason: str) -> dict[str, Any]:
    logger.info("xstocktracker 公司觀點推送略過 | reason=%s", reason)
    return {"pushed": 0, "removed": 0, "skipped": reason}


def _thesis_with_provenance(insight: CompanyInsight) -> str:
    """Label the row's origin inside the text itself.

    X Stock Tracker's company cards are read side by side with its own X-post
    rows; without this a 股癌 thesis reads as if a tweet had said it. The
    `source` column already separates them structurally, but the thesis is
    what a person actually reads. The market Gooaye resolved deterministically
    is carried along here too, since the target table has no market column and
    X re-derives it from its own security masters, which cannot see Gooaye's
    seed-list overrides (TSM is a US listing there, for instance).
    """
    market = (insight.market or "UNKNOWN").strip() or "UNKNOWN"
    header = f"［股癌 Gooaye · {market} · 提及 {int(insight.mention_count or 0)} 集］"
    thesis = (insight.thesis or "").strip()
    return f"{header}\n{thesis}" if thesis else header


def _json_list(value: str | None) -> str:
    """Normalize a stored JSON array, so a corrupted column can't break the push."""
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return "[]"
    return json.dumps(parsed if isinstance(parsed, list) else [], ensure_ascii=False)


def _rows_to_push(selected_symbols: list[str] | None) -> list[dict[str, Any]]:
    with SessionLocal() as session:
        query = select(CompanyInsight)
        if selected_symbols is not None:
            query = query.where(CompanyInsight.symbol.in_(selected_symbols))
        insights = list(session.scalars(query))
    now = datetime.now(timezone.utc)
    rows = []
    for insight in insights:
        symbol = (insight.symbol or "").strip().upper()
        if not symbol or len(symbol) > _SYMBOL_MAX:
            logger.warning("略過無法寫入 xstocktracker 的 symbol | symbol=%r", insight.symbol)
            continue
        rows.append({
            "source": SOURCE_GOOAYE,
            "symbol": symbol,
            "company_name": (insight.company_name or symbol)[:_COMPANY_NAME_MAX],
            "mention_count": int(insight.mention_count or 0),
            "stance": (insight.stance or "neutral")[:_STANCE_MAX],
            "confidence": float(insight.confidence or 0.0),
            "thesis": _thesis_with_provenance(insight),
            "strengths_json": _json_list(insight.strengths_json),
            "weaknesses_json": _json_list(insight.weaknesses_json),
            "evidence_urls_json": _json_list(insight.evidence_urls_json),
            "updated_at": insight.updated_at or now,
        })
    return rows


def _preflight(connection) -> str | None:
    """Return an error string if the target table isn't ready, else None."""
    exists = connection.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name = :table"
    ), {"table": TABLE}).first()
    if not exists:
        return f"{TABLE} 不存在；請先在 X Stock Tracker 執行 python -m scripts.init_postgres"
    has_source = connection.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = 'source'"
    ), {"table": TABLE}).first()
    if not has_source:
        return (f"{TABLE} 還沒有 source 欄位；請先在 X Stock Tracker 執行 "
                "python -m scripts.migrate_company_view_source")
    return None


def push_company_insights(selected_symbols: list[str] | None = None) -> dict[str, Any]:
    """Write Gooaye's company insights into X Stock Tracker's x_company_views.

    `selected_symbols=None` is a full push: every company Gooaye knows about is
    written, and any source='gooaye' row for a company Gooaye no longer tracks
    is deleted. Passing a list restricts both halves to those symbols, which is
    what the sync path uses — it already knows exactly which companies changed,
    and a company whose only mention was dropped on re-analysis is deleted
    there too, so a stale row can't outlive the insight it came from.
    """
    if not push_config().get("push_company_insights", True):
        return _skip("xstocktracker.push_company_insights=false")
    engine = xstocktracker_engine()
    if engine is None:
        return _skip("XSTOCKTRACKER_DB_URL 未設定")

    requested = (sorted({value.strip().upper() for value in selected_symbols if value.strip()})
                 if selected_symbols is not None else None)
    if requested is not None and not requested:
        return _skip("no affected symbols")

    rows = _rows_to_push(requested)
    live_symbols = [row["symbol"] for row in rows]

    try:
        with engine.begin() as connection:
            error = _preflight(connection)
            if error:
                logger.warning("xstocktracker 公司觀點推送略過 | reason=%s", error)
                return {"pushed": 0, "removed": 0, "error": error}

            if rows:
                connection.execute(_UPSERT, rows)

            # Drop rows for companies that no longer have an insight. Scoped to
            # source='gooaye' always, and to the requested symbols on a partial
            # push, so neither X's rows nor Gooaye companies outside this run
            # are ever considered.
            if requested is None:
                removed = connection.execute(text(
                    f"DELETE FROM {TABLE} WHERE source = :source AND NOT (symbol = ANY(:live))"
                ), {"source": SOURCE_GOOAYE, "live": live_symbols}).rowcount
            else:
                stale = sorted(set(requested) - set(live_symbols))
                removed = connection.execute(text(
                    f"DELETE FROM {TABLE} WHERE source = :source AND symbol = ANY(:stale)"
                ), {"source": SOURCE_GOOAYE, "stale": stale}).rowcount if stale else 0
    except SQLAlchemyError as exc:
        # Most likely causes, in order: the gooaye role has not been granted
        # write access to x_company_views yet, or the database is down. Either
        # way Gooaye's own data is already committed; report and move on.
        logger.warning(
            "xstocktracker 公司觀點推送失敗，Gooaye 本地資料不受影響 | error=%s: %s",
            type(exc).__name__, exc,
        )
        return {"pushed": 0, "removed": 0, "error": f"{type(exc).__name__}: {exc}"}

    logger.info(
        "xstocktracker 公司觀點推送完成 | mode=%s | pushed=%d | removed=%d | symbols=%s",
        "incremental" if requested is not None else "all", len(rows), removed, live_symbols,
    )
    return {
        "pushed": len(rows),
        "removed": int(removed or 0),
        "mode": "incremental" if requested is not None else "all",
        "symbols": live_symbols,
    }
