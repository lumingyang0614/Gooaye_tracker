"""Resolve LLM-extracted ticker/company-name strings to real tickers.

Two layers, checked in order:
1. The curated seed list in config/settings.yaml (company_aliases) — small,
   hand-maintained, always available.
2. The optional multi-market security master in app/services/securities.py
   (TW/US/JP/KR, ~19k companies) — used only if XSTOCKTRACKER_DB_URL is set;
   silently skipped otherwise.

LLM output isn't always a clean bare ticker — some providers return things
like "2330 台積電" (code and name glued together) instead of just "2330" or
"2330.TW" (see app/services/llm.py's docstring on glm-5.3-flash's reasoning
quirk, a related but separate issue). So beyond an exact match, this also
splits on whitespace/punctuation and checks each token, then finally checks
whether a known seed alias appears *inside* the raw string, before giving up
and keeping the cleaned string as-is.
"""
from __future__ import annotations

import re

from app.runtime_config import load_runtime_config
from app.services.securities import load_security_lookup

_TOKEN_SPLIT_RE = re.compile(r"[\s,、/()（）\-]+")


def _contains_alias(text: str, alias: str) -> bool:
    if alias.isascii():
        return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.IGNORECASE))
    return alias in text


def _seed_companies() -> list[dict]:
    return load_runtime_config().get("company_aliases", [])


def resolve_company_aliases(text: str) -> list[dict[str, str]]:
    return [company for company in _seed_companies() if any(_contains_alias(text, str(alias)) for alias in company.get("aliases", []))]


def _seed_mapping() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for company in _seed_companies():
        canonical = str(company.get("symbol", "")).upper()
        for alias in [canonical, str(company.get("company_name", "")), *company.get("aliases", [])]:
            if str(alias).strip():
                mapping[str(alias).strip().upper()] = canonical
    return mapping


def canonicalize_symbols(symbols: set[str]) -> set[str]:
    """Map any known alias (ticker or company name) to its canonical symbol."""
    seed_mapping = _seed_mapping()
    seed_companies = _seed_companies()
    external_lookup = load_security_lookup()  # {} if not configured/reachable

    resolved: set[str] = set()
    for raw in symbols:
        cleaned = raw.strip()
        if not cleaned:
            continue
        resolved.add(_resolve_one(cleaned, seed_mapping, seed_companies, external_lookup))
    return resolved


def _resolve_one(
    cleaned: str,
    seed_mapping: dict[str, str],
    seed_companies: list[dict],
    external_lookup: dict[str, dict[str, str]],
) -> str:
    exact = seed_mapping.get(cleaned.upper())
    if exact:
        return exact
    external = external_lookup.get(cleaned.casefold())
    if external:
        return external["symbol"].upper()

    for token in _TOKEN_SPLIT_RE.split(cleaned):
        if not token:
            continue
        if token.upper() in seed_mapping:
            return seed_mapping[token.upper()]
        # Short ASCII tokens (e.g. "SK", "Co") are too likely to collide with
        # an unrelated abbreviation in a ~19k-row external table; only trust
        # a numeric code or a longer/CJK token against that lookup.
        trustworthy = token.isdigit() or not token.isascii() or len(token) >= 3
        if trustworthy and token.casefold() in external_lookup:
            return external_lookup[token.casefold()]["symbol"].upper()

    match = next(
        (company for company in seed_companies if any(
            _contains_alias(cleaned, str(alias))
            for alias in [company.get("symbol", ""), company.get("company_name", ""), *company.get("aliases", [])]
            if str(alias).strip()
        )),
        None,
    )
    return str(match["symbol"]).upper() if match else cleaned.upper()
