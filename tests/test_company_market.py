"""Market classification for company insights. Pure functions; no DB or LLM."""
import pytest

from app.services import entity_resolver
from app.services.entity_resolver import resolve_market


@pytest.fixture(autouse=True)
def offline_lookups(monkeypatch):
    """resolve_market consults the two security masters; default them to empty
    so each test opts in to exactly the rows it cares about."""
    monkeypatch.setattr(entity_resolver, "load_taiwan_security_lookup", lambda: {})
    monkeypatch.setattr(entity_resolver, "load_security_lookup", lambda: {})


def test_taiwan_suffix_is_taiwan():
    assert resolve_market("2330.TW") == "TW"


@pytest.mark.parametrize("symbol", ["005930.KS", "2802.T", "300476.SZ", "LPK.DE", "SIVE.ST"])
def test_foreign_suffixes_group_as_other(symbol):
    assert resolve_market(symbol) == "OTHER"


@pytest.mark.parametrize("symbol", ["NVDA", "MU", "GOOGL", "AMD"])
def test_bare_ascii_ticker_is_us(symbol):
    assert resolve_market(symbol) == "US"


def test_unresolved_chinese_name_stays_unknown():
    # Nothing resolved it to a ticker, so it must not be guessed into a market;
    # it belongs in the 待確認 filter for review.
    assert resolve_market("文曄") == "UNKNOWN"


def test_bare_digits_are_not_assumed_taiwanese():
    # A real Taiwan listing would already have matched the security master, so
    # a leftover 4-digit code is more likely Japanese or Chinese.
    assert resolve_market("2802") == "UNKNOWN"


def test_taiwan_master_classifies_symbol_it_knows(monkeypatch):
    monkeypatch.setattr(
        entity_resolver,
        "load_taiwan_security_lookup",
        lambda: {"3036.tw": {"symbol": "3036.TW", "company_name": "文曄", "market": "TW"}},
    )
    assert resolve_market("3036.TW") == "TW"


def test_seed_market_overrides_symbol_shape(monkeypatch):
    # TSM looks like a plain US ticker and is one — but the override is what
    # makes that explicit rather than incidental.
    monkeypatch.setattr(
        entity_resolver,
        "load_runtime_config",
        lambda: {"company_aliases": [{"symbol": "TSM", "company_name": "台積電 ADR", "market": "US"}]},
    )
    assert resolve_market("TSM") == "US"


def test_seed_override_can_reclassify_a_taiwan_suffix(monkeypatch):
    monkeypatch.setattr(
        entity_resolver,
        "load_runtime_config",
        lambda: {"company_aliases": [{"symbol": "2330.TW", "company_name": "台積電", "market": "OTHER"}]},
    )
    # The seed list is checked before the suffix rule, so a hand-verified entry
    # always wins over the shape of the symbol.
    assert resolve_market("2330.TW") == "OTHER"


def test_invalid_seed_market_is_ignored(monkeypatch):
    monkeypatch.setattr(
        entity_resolver,
        "load_runtime_config",
        lambda: {"company_aliases": [{"symbol": "2330.TW", "market": "台股"}]},
    )
    assert resolve_market("2330.TW") == "TW"


def test_blank_symbol_is_unknown():
    assert resolve_market("   ") == "UNKNOWN"
