"""Sync the official TWSE (上市) + TPEx (上櫃) security master.

Both exchanges publish an unauthenticated OpenAPI listing endpoint, so this
needs no key and no scraping. It exists to replace the hand-maintained seed
list in config/settings.yaml as the *bulk* source of Taiwan company-name ->
ticker knowledge: 股癌 mentions far more Taiwanese companies than anyone will
keep in a YAML file by hand, and without the master table those mentions get
stored as raw Chinese strings that never join up with anything.

The seed list stays authoritative on top of this (see entity_resolver.py) —
it carries the hand-verified nicknames and the non-Taiwan companies, neither
of which the exchanges know about.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import httpx
from sqlalchemy import select, update

from app.db import SessionLocal, init_db
from app.models import TaiwanSecurity
from app.services.company_names import index_alias

logger = logging.getLogger("uvicorn.error")

TWSE_COMPANIES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANIES_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes"


async def _fetch_rows(client: httpx.AsyncClient, url: str) -> list[dict[str, Any]]:
    """Fetch one exchange's JSON listing.

    Decoded explicitly as UTF-8 rather than trusting httpx's charset guess:
    TPEx replies without a charset in Content-Type, and the fallback guess
    mangles every Chinese company name.
    """
    response = await client.get(url)
    response.raise_for_status()
    payload = json.loads(response.content.decode("utf-8"))
    return payload if isinstance(payload, list) else []


def _twse_row(row: dict[str, Any]) -> dict[str, str] | None:
    code = str(row.get("公司代號") or "").strip()
    if not (code.isdigit() and len(code) == 4):
        return None  # skip ETFs, warrants and other non-4-digit instruments
    return {
        "code": code,
        "canonical_symbol": f"{code}.TW",
        "provider_symbol": f"{code}.TW",
        "company_name": str(row.get("公司簡稱") or row.get("公司名稱") or code).strip(),
        "english_name": str(row.get("英文簡稱") or "").strip(),
        "exchange": "TWSE",
        "industry": str(row.get("產業別") or "").strip(),
        "source": "TWSE OpenAPI",
    }


def _tpex_row(row: dict[str, Any]) -> dict[str, str] | None:
    code = str(row.get("SecuritiesCompanyCode") or "").strip()
    if not (code.isdigit() and len(code) == 4):
        return None
    return {
        "code": code,
        "canonical_symbol": f"{code}.TW",
        # 上櫃 companies quote as .TWO at every vendor even though this
        # project's canonical identity keeps the plain .TW suffix.
        "provider_symbol": f"{code}.TWO",
        "company_name": str(row.get("CompanyName") or code).strip(),
        "english_name": "",
        "exchange": "TPEx",
        "industry": "",
        "source": "TPEx OpenAPI",
    }


async def fetch_taiwan_security_master() -> list[dict[str, str]]:
    """Fetch both exchanges, or raise.

    Each exchange is validated on its own rather than only checking the merged
    result. The two endpoints fail independently, and a sync that accepted
    "TWSE fine, TPEx empty" would go on to deactivate every 上櫃 company in the
    master — silently losing ticker resolution for all of them while reporting
    success. Aborting the whole batch leaves the previous master in place,
    which is always better than a half one.
    """
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        twse_rows, tpex_rows = await asyncio.gather(
            _fetch_rows(client, TWSE_COMPANIES_URL),
            _fetch_rows(client, TPEX_COMPANIES_URL),
        )
    securities: dict[str, dict[str, str]] = {}
    parsed_counts = {"TWSE": 0, "TPEx": 0}
    for raw, parse in ((twse_rows, _twse_row), (tpex_rows, _tpex_row)):
        for row in raw:
            item = parse(row)
            if item:
                securities[item["code"]] = item
                parsed_counts[item["exchange"]] += 1
    empty = sorted(name for name, count in parsed_counts.items() if not count)
    if empty:
        raise RuntimeError(
            f"沒有取得任何有效證券資料，中止同步以保留既有主檔 | 來源={'、'.join(empty)}"
        )
    return list(securities.values())


async def sync_taiwan_securities() -> dict[str, int]:
    # fetch_taiwan_security_master raises before anything here touches the
    # database, so a failed fetch can never leave a partially deactivated table.
    rows = await fetch_taiwan_security_master()

    init_db()
    now = datetime.now(timezone.utc)
    counts = {"TWSE": 0, "TPEx": 0}
    with SessionLocal() as session:
        # Mark everything inactive first, then reactivate what came back, so
        # delisted companies drop out of the lookup instead of lingering.
        session.execute(update(TaiwanSecurity).values(is_active=0))
        for item in rows:
            record = session.scalar(select(TaiwanSecurity).where(TaiwanSecurity.code == item["code"]))
            if record is None:
                record = TaiwanSecurity(code=item["code"])
                session.add(record)
            for key, value in item.items():
                setattr(record, key, value)
            record.is_active = 1
            record.updated_at = now
            counts[item["exchange"]] += 1
        session.commit()

    load_taiwan_security_lookup.cache_clear()
    logger.info("台股證券主檔同步完成 | total=%s | twse=%s | tpex=%s", len(rows), counts["TWSE"], counts["TPEx"])
    return {"total": len(rows), "twse": counts["TWSE"], "tpex": counts["TPEx"]}


def _lookup_from_rows(rows: list[TaiwanSecurity]) -> dict[str, dict[str, str]]:
    """Build an alias -> company map, dropping aliases claimed by two
    companies rather than guessing which one was meant."""
    entries: dict[str, dict[str, str]] = {}
    candidates: dict[str, set[str]] = {}
    for row in rows:
        item = {
            "symbol": row.canonical_symbol,
            "company_name": row.company_name,
            "market": "TW",
            "exchange": row.exchange,
            "provider_symbol": row.provider_symbol,
        }
        entries[row.canonical_symbol.casefold()] = item
        for alias in (row.code, row.provider_symbol, row.company_name, row.english_name):
            index_alias(candidates, alias, row.canonical_symbol)

    lookup = dict(entries)
    for alias, symbols in candidates.items():
        if alias in lookup or len(symbols) > 1:
            continue
        (only,) = symbols
        lookup[alias] = entries[only.casefold()]
    return lookup


@lru_cache
def load_taiwan_security_lookup() -> dict[str, dict[str, str]]:
    """Alias -> {symbol, company_name, market, exchange, provider_symbol}.

    Cached for the process lifetime; sync_taiwan_securities clears it. Returns
    {} if the table is missing or empty so callers degrade to the seed list
    instead of failing.
    """
    try:
        with SessionLocal() as session:
            rows = list(session.scalars(select(TaiwanSecurity).where(TaiwanSecurity.is_active == 1)))
    except Exception as exc:
        logger.warning("台股證券主檔無法載入，改用種子別名清單 | error=%s: %s", type(exc).__name__, exc)
        return {}
    if not rows:
        return {}
    lookup = _lookup_from_rows(rows)
    logger.info("台股證券主檔載入完成 | companies=%s | aliases=%s", len(rows), len(lookup))
    return lookup
