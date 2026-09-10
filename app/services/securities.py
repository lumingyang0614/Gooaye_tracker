"""Optional read-only lookup against a pre-existing multi-market security
master database (table shape: code, canonical_symbol, provider_symbol,
company_name, english_name, market, is_active — the same shape Serenity Stock
Tracker's own taiwan_securities table uses).

This is a separate Postgres database from Gooaye's own (set via
XSTOCKTRACKER_DB_URL), read with its own SQLAlchemy engine. It exists purely
to canonicalize company names the LLM extracts ("台積電", "NVIDIA") into real
tickers across Taiwan/US/Japan/Korea, without Gooaye having to maintain its
own full security master. If the URL isn't configured, or the database isn't
reachable, this degrades to an empty lookup — callers fall back to the small
seed list in config/settings.yaml instead of failing.
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text

from app.config import get_settings

logger = logging.getLogger("uvicorn.error")

_TABLES = ("taiwan_securities", "us_securities", "japan_securities", "korea_securities")


def _psycopg_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


@lru_cache
def xstocktracker_engine():
    """The shared engine for the X Stock Tracker database, or None if
    XSTOCKTRACKER_DB_URL isn't set.

    Cached so the security-master lookups here and the company-view push in
    app/services/xstock_push.py use one connection pool between them rather
    than opening a second one against the same database.
    """
    url = get_settings().xstocktracker_db_url.strip()
    if not url:
        return None
    return create_engine(_psycopg_dsn(url), pool_pre_ping=True)


@lru_cache
def load_security_lookup() -> dict[str, dict[str, str]]:
    """Build an alias -> {symbol, company_name, market} map from every
    configured security table. Cached for the lifetime of the process; call
    load_security_lookup.cache_clear() to force a refresh.

    canonical_symbol always carries a market suffix (2330.TW vs 7203.T), so it
    can never collide across markets and is indexed directly. Bare codes,
    provider symbols, and company/English names carry no such suffix — Taiwan
    and Japan both hand out plain 4-digit codes, so e.g. "2337" is a real
    collision (confirmed against this database: 15 bare codes are shared by
    two markets). Any alias that resolves to more than one distinct symbol is
    dropped rather than guessed at.
    """
    engine = xstocktracker_engine()
    if engine is None:
        return {}
    canonical_entries: dict[str, dict[str, str]] = {}
    candidates: dict[str, set[str]] = {}
    try:
        with engine.connect() as connection:
            for table in _TABLES:
                columns = {row[0] for row in connection.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
                    {"table": table},
                )}
                if not columns:
                    continue
                has_english_name = "english_name" in columns
                select_cols = "code, canonical_symbol, provider_symbol, company_name, market" + (", english_name" if has_english_name else "")
                rows = connection.execute(text(f"SELECT {select_cols} FROM {table} WHERE is_active = 1"))
                for row in rows:
                    data = row._mapping
                    symbol = str(data.get("canonical_symbol") or "").strip()
                    if not symbol:
                        continue
                    item = {
                        "symbol": symbol,
                        "company_name": str(data.get("company_name") or "").strip(),
                        "market": str(data.get("market") or "").strip(),
                    }
                    canonical_entries[symbol.casefold()] = item
                    for key in ("code", "provider_symbol", "company_name", "english_name" if has_english_name else None):
                        if not key:
                            continue
                        value = data.get(key)
                        if value and str(value).strip():
                            candidates.setdefault(str(value).strip().casefold(), set()).add(symbol)

        lookup: dict[str, dict[str, str]] = dict(canonical_entries)
        ambiguous = 0
        for alias, symbols in candidates.items():
            if alias in lookup:
                continue  # a canonical_symbol already claims this key; leave it alone
            if len(symbols) > 1:
                ambiguous += 1
                continue
            (only_symbol,) = symbols
            lookup[alias] = canonical_entries.get(only_symbol.casefold(), {"symbol": only_symbol, "company_name": "", "market": ""})
        logger.info(
            "xstocktracker 證券主檔載入完成 | aliases=%s | ambiguous_dropped=%s",
            len(lookup), ambiguous,
        )
    except Exception as exc:
        logger.warning("xstocktracker 證券主檔無法載入，改用種子別名清單 | error=%s: %s", type(exc).__name__, exc)
        return {}
    return lookup


def lookup_symbol(name_or_code: str) -> dict[str, str] | None:
    return load_security_lookup().get(name_or_code.strip().casefold())
