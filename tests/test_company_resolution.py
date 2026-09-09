"""Company grouping resolves stored tickers again at read time.

Regression cover for two defects: segments analyzed before the security master
existed stayed stuck under the raw name the model produced, and company names
came only from the YAML seed list so master-only companies displayed as a bare
ticker.
"""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db
from app.models import CompanyInsight, Episode, EpisodeSegment
from app.services import company_insights as insights


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(insights, "SessionLocal", factory)
    monkeypatch.setattr(insights, "resolve_market", lambda symbol: "TW")
    monkeypatch.setattr(insights, "load_runtime_config", lambda: {})
    monkeypatch.setattr(insights, "load_taiwan_security_lookup", lambda: {})
    db.init_db()
    yield factory
    engine.dispose()


def _segment(factory, tickers, number=1):
    with factory() as session:
        episode = Episode(
            episode_number=number, title=f"EP{number}",
            published_at=datetime.now(timezone.utc), source_url=f"https://example.com/{number}",
        )
        session.add(episode)
        session.flush()
        session.add(EpisodeSegment(
            episode_id=episode.id, chunk_index=0, tickers=tickers,
            sentiment="bullish", importance=0.5, summary="summary", reason="reason",
        ))
        session.commit()


def test_rebuild_regroups_a_historical_raw_name_onto_its_canonical_symbol(database, monkeypatch):
    # Analyzed before scripts.sync_taiwan_securities existed, so the stored
    # string is the name the model said, not a ticker.
    _segment(database, "文曄")
    monkeypatch.setattr(insights, "canonicalize_symbol_map", lambda values: {"文曄": "3036.TW"})

    asyncio.run(insights.rebuild_company_insights())

    assert [row["symbol"] for row in insights.list_company_insights()] == ["3036.TW"]


def test_old_and_new_mentions_of_one_company_merge_into_a_single_row(database, monkeypatch):
    _segment(database, "文曄", number=1)
    _segment(database, "3036.TW", number=2)
    monkeypatch.setattr(insights, "canonicalize_symbol_map",
                        lambda values: {value: "3036.TW" for value in values})

    asyncio.run(insights.rebuild_company_insights())

    rows = insights.list_company_insights()
    assert [row["symbol"] for row in rows] == ["3036.TW"]
    assert rows[0]["mention_count"] == 2
    # The drill-down has to agree with the grouping, or the evidence link on a
    # merged company would open an empty panel.
    assert len(insights.list_symbol_segments("3036.TW")) == 2


def test_one_row_mentioning_both_spellings_is_not_double_counted(database, monkeypatch):
    _segment(database, "文曄,3036.TW")
    monkeypatch.setattr(insights, "canonicalize_symbol_map",
                        lambda values: {value: "3036.TW" for value in values})

    asyncio.run(insights.rebuild_company_insights())

    (row,) = insights.list_company_insights()
    assert row["mention_count"] == 1
    assert len(insights.list_symbol_segments("3036.TW")) == 1


def test_unresolvable_ticker_still_groups_under_itself(database, monkeypatch):
    _segment(database, "unknown corp")
    monkeypatch.setattr(insights, "canonicalize_symbol_map", lambda values: {})

    asyncio.run(insights.rebuild_company_insights())

    assert [row["symbol"] for row in insights.list_company_insights()] == ["UNKNOWN CORP"]


def test_company_name_falls_back_to_the_security_master(database, monkeypatch):
    _segment(database, "3036.TW")
    monkeypatch.setattr(insights, "canonicalize_symbol_map", lambda values: {"3036.TW": "3036.TW"})
    monkeypatch.setattr(insights, "load_taiwan_security_lookup", lambda: {
        "3036.tw": {"symbol": "3036.TW", "company_name": "文曄", "market": "TW"},
    })

    asyncio.run(insights.rebuild_company_insights())

    with database() as session:
        assert session.scalar(select(CompanyInsight)).company_name == "文曄"


def test_seed_list_name_overrides_the_master(database, monkeypatch):
    _segment(database, "2330.TW")
    monkeypatch.setattr(insights, "canonicalize_symbol_map", lambda values: {"2330.TW": "2330.TW"})
    monkeypatch.setattr(insights, "load_taiwan_security_lookup", lambda: {
        "2330.tw": {"symbol": "2330.TW", "company_name": "台灣積體電路製造", "market": "TW"},
    })
    monkeypatch.setattr(insights, "load_runtime_config", lambda: {
        "company_aliases": [{"symbol": "2330.TW", "company_name": "台積電"}],
    })

    asyncio.run(insights.rebuild_company_insights())

    with database() as session:
        assert session.scalar(select(CompanyInsight)).company_name == "台積電"


def _legacy_row(factory, symbol, name=""):
    """A CompanyInsight written before the security master knew this company."""
    with factory() as session:
        session.add(CompanyInsight(
            symbol=symbol, company_name=name or symbol,
            mention_count=1, stance="bullish", confidence=0.5,
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()


def _alias_map(monkeypatch, mapping):
    monkeypatch.setattr(insights, "canonicalize_symbol_map",
                        lambda values: {value: mapping.get(value, value.upper()) for value in values})


def test_partial_rebuild_removes_the_row_left_under_the_old_name(database, monkeypatch):
    # The sync path only ever passes the symbols an episode touched, so if it
    # cannot clean this up the company stays on screen twice forever.
    _segment(database, "文曄")
    _legacy_row(database, "文曄")
    _alias_map(monkeypatch, {"文曄": "3036.TW", "3036.TW": "3036.TW"})

    result = asyncio.run(insights.rebuild_company_insights(selected_symbols=["3036.TW"]))

    assert result["symbols_removed"] == 1
    assert [row["symbol"] for row in insights.list_company_insights()] == ["3036.TW"]


def test_partial_rebuild_given_the_old_name_still_builds_the_canonical_company(database, monkeypatch):
    _segment(database, "文曄")
    _legacy_row(database, "文曄")
    _alias_map(monkeypatch, {"文曄": "3036.TW", "3036.TW": "3036.TW"})

    asyncio.run(insights.rebuild_company_insights(selected_symbols=["文曄"]))

    assert [row["symbol"] for row in insights.list_company_insights()] == ["3036.TW"]


def test_partial_rebuild_leaves_unrelated_companies_alone(database, monkeypatch):
    _segment(database, "文曄", number=1)
    _legacy_row(database, "文曄")
    _legacy_row(database, "2330.TW", "台積電")
    _alias_map(monkeypatch, {"文曄": "3036.TW", "3036.TW": "3036.TW"})

    asyncio.run(insights.rebuild_company_insights(selected_symbols=["3036.TW"]))

    # 2330.TW has no segments either, but it was not part of this rebuild's
    # scope — deleting it here would make a targeted rebuild destructive.
    assert sorted(row["symbol"] for row in insights.list_company_insights()) == ["2330.TW", "3036.TW"]


def test_partial_rebuild_with_only_blank_symbols_does_nothing(database, monkeypatch):
    _segment(database, "3036.TW")
    _legacy_row(database, "3036.TW")
    _alias_map(monkeypatch, {"3036.TW": "3036.TW"})

    result = asyncio.run(insights.rebuild_company_insights(selected_symbols=["", "   "]))

    assert (result["symbols_considered"], result["symbols_removed"]) == (0, 0)
    assert [row["symbol"] for row in insights.list_company_insights()] == ["3036.TW"]


def test_two_old_names_of_one_company_are_both_cleaned_up(database, monkeypatch):
    _segment(database, "3036.TW")
    _legacy_row(database, "文曄")
    _legacy_row(database, "WT MICROELECTRONICS")
    _alias_map(monkeypatch, {value: "3036.TW" for value in ("文曄", "WT MICROELECTRONICS", "3036.TW")})

    result = asyncio.run(insights.rebuild_company_insights(selected_symbols=["3036.TW"]))

    assert result["symbols_removed"] == 2
    assert [row["symbol"] for row in insights.list_company_insights()] == ["3036.TW"]
