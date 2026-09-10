"""Resolve LLM-extracted ticker/company-name strings to real tickers, and
classify each resolved ticker into a market.

Three lookup layers, checked in order:
1. The curated seed list in config/settings.yaml (company_aliases) — small,
   hand-maintained, carries nicknames and non-Taiwan companies the exchanges
   don't publish. Always available.
2. The official TWSE/TPEx security master in app/services/taiwan_securities.py
   — every listed Taiwanese company, synced from the exchanges' own OpenAPI.
   Empty until `python -m scripts.sync_taiwan_securities` has been run.
3. The optional multi-market security master in app/services/securities.py
   (TW/US/JP/KR, ~19k companies) — used only if XSTOCKTRACKER_DB_URL is set;
   silently skipped otherwise.

Taiwan is checked before the multi-market table on purpose: 股癌 is a Taiwanese
show, so a bare 4-digit code is overwhelmingly a Taiwan listing, while the
multi-market table drops exactly those codes as ambiguous (Taiwan and Japan
both issue plain 4-digit codes — see securities.py). Without the Taiwan-first
layer those mentions would resolve to nothing at all.

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
from app.services.taiwan_securities import load_taiwan_security_lookup

_TOKEN_SPLIT_RE = re.compile(r"[\s,、/()（）\-]+")

# Ticker suffixes seen in this project's seed list and in LLM output. Anything
# with a market suffix that isn't Taiwan is grouped as OTHER: the dashboard's
# job is to separate 台股 from everything else, not to run a global exchange
# directory. Values match Serenity Stock Tracker's market enum.
_SUFFIX_MARKETS = {
    ".TW": "TW", ".TWO": "TW",
    ".T": "OTHER", ".KS": "OTHER", ".KQ": "OTHER", ".HK": "OTHER",
    ".SS": "OTHER", ".SZ": "OTHER", ".DE": "OTHER", ".ST": "OTHER",
    ".PA": "OTHER", ".L": "OTHER", ".AS": "OTHER", ".SW": "OTHER",
}
_VALID_MARKETS = {"TW", "US", "OTHER", "UNKNOWN"}


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


def canonicalize_symbol_map(symbols: set[str]) -> dict[str, str]:
    """Map each input alias to its canonical symbol, keyed by the input.

    The lookup tables are built once for the whole batch, so callers holding
    many raw strings (every stored segment's tickers, say) pay for them once.
    Callers that need to rewrite rows in place, or to regroup existing rows
    under their canonical symbol, need to know which input produced which
    result — `canonicalize_symbols` throws that away.
    """
    seed_mapping = _seed_mapping()
    seed_companies = _seed_companies()
    taiwan_lookup = load_taiwan_security_lookup()  # {} until the master is synced
    external_lookup = load_security_lookup()  # {} if not configured/reachable

    resolved: dict[str, str] = {}
    for raw in symbols:
        cleaned = raw.strip()
        if not cleaned:
            continue
        resolved[raw] = _resolve_one(cleaned, seed_mapping, seed_companies, taiwan_lookup, external_lookup)
    return resolved


def canonicalize_symbols(symbols: set[str]) -> set[str]:
    """Map any known alias (ticker or company name) to its canonical symbol."""
    return set(canonicalize_symbol_map(symbols).values())


def _resolve_one(
    cleaned: str,
    seed_mapping: dict[str, str],
    seed_companies: list[dict],
    taiwan_lookup: dict[str, dict[str, str]],
    external_lookup: dict[str, dict[str, str]],
) -> str:
    exact = seed_mapping.get(cleaned.upper())
    if exact:
        return exact
    taiwan = taiwan_lookup.get(cleaned.casefold())
    if taiwan:
        return taiwan["symbol"].upper()
    external = external_lookup.get(cleaned.casefold())
    if external:
        return external["symbol"].upper()

    for token in _TOKEN_SPLIT_RE.split(cleaned):
        if not token:
            continue
        if token.upper() in seed_mapping:
            return seed_mapping[token.upper()]
        # Short ASCII tokens (e.g. "SK", "Co") are too likely to collide with
        # an unrelated abbreviation in a large security table; only trust
        # a numeric code or a longer/CJK token against those lookups.
        trustworthy = token.isdigit() or not token.isascii() or len(token) >= 3
        if not trustworthy:
            continue
        if token.casefold() in taiwan_lookup:
            return taiwan_lookup[token.casefold()]["symbol"].upper()
        if token.casefold() in external_lookup:
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


def _seed_markets() -> dict[str, str]:
    """Explicit `market:` values from config/settings.yaml's company_aliases.

    Only needed where the symbol alone is misleading — TSM trades in New York
    despite being a Taiwanese company, and 股癌 discusses it as a US listing.
    """
    markets: dict[str, str] = {}
    for company in _seed_companies():
        symbol = str(company.get("symbol", "")).strip().upper()
        market = str(company.get("market", "")).strip().upper()
        if symbol and market in _VALID_MARKETS:
            markets[symbol] = market
    return markets


def resolve_market(symbol: str) -> str:
    """Classify a canonical symbol into TW / US / OTHER / UNKNOWN.

    Deliberately deterministic rather than LLM-decided: asking a model
    "is 2802 Taiwanese or Japanese?" is how a Taiwan-focused board ends up
    full of foreign numeric codes. Unresolvable strings stay UNKNOWN so they
    surface in the 待確認 filter for review instead of being guessed into a
    market they may not belong to.
    """
    cleaned = symbol.strip()
    if not cleaned:
        return "UNKNOWN"
    upper = cleaned.upper()

    seed_market = _seed_markets().get(upper)
    if seed_market:
        return seed_market

    for suffix, market in _SUFFIX_MARKETS.items():
        if upper.endswith(suffix):
            return market

    if upper.casefold() in load_taiwan_security_lookup():
        return "TW"

    external = load_security_lookup().get(upper.casefold())
    if external:
        external_market = str(external.get("market", "")).strip().upper()
        if external_market == "TW":
            return "TW"
        if external_market == "US":
            return "US"
        if external_market:
            return "OTHER"

    # Bare symbols require a trusted seed or master entry. Names and platforms
    # can have the same spelling shape as US tickers; leave them unconfirmed.
    # Shape alone cannot distinguish a ticker from a platform such as STEAM.
    known_seed_symbols = {str(c.get("symbol", "")).strip().upper() for c in _seed_companies()}
    if upper in known_seed_symbols and upper.isascii() and upper.isalpha() and 1 <= len(upper) <= 5:
        return "US"
    return "UNKNOWN"
