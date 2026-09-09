"""TWSE/TPEx security master parsing and alias building. No network, no DB."""
import asyncio

import pytest

from app.models import TaiwanSecurity
from app.services import taiwan_securities as ts
from app.services.taiwan_securities import _lookup_from_rows, _tpex_row, _twse_row


def test_twse_row_uses_short_name_and_keeps_tw_provider_symbol():
    row = _twse_row({
        "公司代號": "2330", "公司簡稱": "台積電", "公司名稱": "台灣積體電路製造股份有限公司",
        "英文簡稱": "TSMC", "產業別": "半導體業",
    })
    assert row["canonical_symbol"] == "2330.TW"
    assert row["provider_symbol"] == "2330.TW"
    assert row["company_name"] == "台積電"
    assert row["english_name"] == "TSMC"
    assert row["exchange"] == "TWSE"


def test_tpex_row_quotes_as_two_but_keeps_tw_identity():
    row = _tpex_row({"SecuritiesCompanyCode": "3363", "CompanyName": "上詮"})
    # 上櫃 companies are .TWO at quote vendors while this project's canonical
    # identity stays .TW — losing that distinction breaks price lookups later.
    assert (row["canonical_symbol"], row["provider_symbol"]) == ("3363.TW", "3363.TWO")
    assert row["exchange"] == "TPEx"


def test_non_four_digit_instruments_are_skipped():
    assert _twse_row({"公司代號": "0050", "公司簡稱": "元大台灣50"})["code"] == "0050"
    assert _twse_row({"公司代號": "00878B", "公司簡稱": "某債券 ETF"}) is None
    assert _tpex_row({"SecuritiesCompanyCode": "", "CompanyName": "空白"}) is None


def _security(code, name, english_name="", exchange="TWSE"):
    return TaiwanSecurity(
        code=code, canonical_symbol=f"{code}.TW", provider_symbol=f"{code}.TW",
        company_name=name, english_name=english_name, exchange=exchange,
    )


def test_lookup_indexes_code_name_and_english_name():
    lookup = _lookup_from_rows([_security("2330", "台積電", "TSMC")])
    for alias in ("2330", "2330.tw", "台積電", "tsmc"):
        assert lookup[alias]["symbol"] == "2330.TW"


def test_alias_claimed_by_two_companies_is_dropped_not_guessed():
    lookup = _lookup_from_rows([
        _security("1234", "同名企業"),
        _security("5678", "同名企業"),
    ])
    assert "同名企業" not in lookup
    # The unambiguous keys for each company still resolve.
    assert lookup["1234"]["symbol"] == "1234.TW"
    assert lookup["5678"]["symbol"] == "5678.TW"


def test_canonical_symbol_is_never_displaced_by_another_companys_alias():
    lookup = _lookup_from_rows([
        _security("2330", "台積電"),
        # A company whose name happens to spell another company's symbol.
        _security("9999", "2330.TW"),
    ])
    assert lookup["2330.tw"]["company_name"] == "台積電"


def test_partial_exchange_outage_aborts_before_touching_the_master(monkeypatch):
    """TWSE fine + TPEx empty must raise, not deactivate every 上櫃 company.

    sync_taiwan_securities marks the whole table inactive before reactivating
    what came back, so accepting a half response would silently drop every
    上櫃 ticker from resolution while reporting success.
    """
    async def fetch(client, url):
        return [{"公司代號": "2330", "公司簡稱": "台積電"}] if url == ts.TWSE_COMPANIES_URL else []

    monkeypatch.setattr(ts, "_fetch_rows", fetch)
    with pytest.raises(RuntimeError, match="TPEx"):
        asyncio.run(ts.fetch_taiwan_security_master())


def test_both_exchanges_empty_also_aborts(monkeypatch):
    async def fetch(client, url):
        return []

    monkeypatch.setattr(ts, "_fetch_rows", fetch)
    with pytest.raises(RuntimeError):
        asyncio.run(ts.fetch_taiwan_security_master())


def test_both_exchanges_present_returns_the_merged_master(monkeypatch):
    async def fetch(client, url):
        if url == ts.TWSE_COMPANIES_URL:
            return [{"公司代號": "2330", "公司簡稱": "台積電"}]
        return [{"SecuritiesCompanyCode": "3363", "CompanyName": "上詮"}]

    monkeypatch.setattr(ts, "_fetch_rows", fetch)
    rows = asyncio.run(ts.fetch_taiwan_security_master())
    assert {row["canonical_symbol"] for row in rows} == {"2330.TW", "3363.TW"}
