"""Maintenance scripts must not destroy the data they are maintaining."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import scripts
from app.models import Base, CompanyInsight, Episode, EpisodeSegment
from scripts import backfill_segment_tickers as backfill
from scripts import migrate_company_market as migration


@pytest.fixture
def database(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (migration, backfill):
        monkeypatch.setattr(module, "SessionLocal", factory)
        monkeypatch.setattr(module, "init_db", lambda: None)
    # The ALTER runs against PostgreSQL's information_schema; the column is
    # already present on a freshly created SQLite schema, so skip the DDL and
    # exercise the backfill, which is where the data loss happened.
    monkeypatch.setattr(migration, "add_column", lambda: False)
    monkeypatch.setattr(migration, "resolve_market", lambda symbol: "TW")
    yield factory
    engine.dispose()


def test_migration_keeps_a_company_that_has_no_segments(database):
    # Any database predating the segments table is entirely made of these, so
    # a migration that dropped them would wipe every company it migrated.
    with database() as session:
        session.add(CompanyInsight(
            symbol="2330.TW", company_name="台積電", thesis="existing analysis",
            strengths_json='["existing strength"]', weaknesses_json='["existing weakness"]',
            mention_count=7, stance="bullish", confidence=0.8,
            updated_at=datetime.now(timezone.utc),
        ))
        session.commit()

    asyncio.run(migration.main())

    with database() as session:
        row = session.scalar(select(CompanyInsight))
    assert row is not None, "migration deleted an existing company"
    assert row.thesis == "existing analysis"
    assert row.strengths_json == '["existing strength"]'
    assert row.weaknesses_json == '["existing weakness"]'
    assert row.mention_count == 7
    assert row.market == "TW"


def test_migration_is_idempotent(database):
    with database() as session:
        session.add(CompanyInsight(symbol="2330.TW", thesis="keep", updated_at=datetime.now(timezone.utc)))
        session.commit()

    asyncio.run(migration.main())
    asyncio.run(migration.main())

    with database() as session:
        row = session.scalar(select(CompanyInsight))
    assert (row.thesis, row.market) == ("keep", "TW")


def _episode_with_segment(factory, tickers, number=1):
    with factory() as session:
        episode = Episode(
            episode_number=number, title=f"EP{number}", tickers=tickers,
            published_at=datetime.now(timezone.utc), source_url=f"https://example.com/{number}",
        )
        session.add(episode)
        session.flush()
        session.add(EpisodeSegment(
            episode_id=episode.id, chunk_index=0, tickers=tickers,
            sentiment="bullish", importance=0.5, summary="summary",
        ))
        session.commit()


def test_backfill_rewrites_stored_tickers_on_both_tables(database, monkeypatch):
    _episode_with_segment(database, "文曄")
    monkeypatch.setattr(backfill, "canonicalize_symbol_map", lambda values: {"文曄": "3036.TW"})

    result = backfill.backfill()

    assert (result["segments"], result["episodes"]) == (1, 1)
    with database() as session:
        assert session.scalar(select(EpisodeSegment)).tickers == "3036.TW"
        assert session.scalar(select(Episode)).tickers == "3036.TW"


def test_backfill_collapses_two_spellings_of_one_company(database, monkeypatch):
    _episode_with_segment(database, "文曄,3036.TW")
    monkeypatch.setattr(backfill, "canonicalize_symbol_map",
                        lambda values: {value: "3036.TW" for value in values})

    backfill.backfill()

    with database() as session:
        assert session.scalar(select(EpisodeSegment)).tickers == "3036.TW"


def test_backfill_dry_run_writes_nothing(database, monkeypatch):
    _episode_with_segment(database, "文曄")
    monkeypatch.setattr(backfill, "canonicalize_symbol_map", lambda values: {"文曄": "3036.TW"})

    result = backfill.backfill(dry_run=True)

    assert result["segments"] == 1
    with database() as session:
        assert session.scalar(select(EpisodeSegment)).tickers == "文曄"


def test_backfill_reports_nothing_to_do_when_already_canonical(database, monkeypatch):
    _episode_with_segment(database, "3036.TW")
    monkeypatch.setattr(backfill, "canonicalize_symbol_map", lambda values: {"3036.TW": "3036.TW"})

    assert backfill.backfill() == {"segments": 0, "episodes": 0, "distinct_symbols_seen": 1}


class _Reconfigurable:
    def __init__(self):
        self.calls = []

    def reconfigure(self, **kwargs):
        self.calls.append(kwargs)


class _Raising(_Reconfigurable):
    def reconfigure(self, **kwargs):
        raise ValueError("stream does not support reconfiguration")


def test_console_is_pinned_to_utf8_for_script_output(monkeypatch):
    """Sync summaries print affected_symbols, which can hold an unresolved
    Simplified Chinese company name that cp950 cannot encode — losing a run
    that had already completed all of its work."""
    stdout, stderr = _Reconfigurable(), _Reconfigurable()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)

    scripts._force_utf8_console()

    assert stdout.calls == [{"encoding": "utf-8", "errors": "replace"}]
    assert stderr.calls == [{"encoding": "utf-8", "errors": "replace"}]


@pytest.mark.parametrize("stream", [object(), _Raising()])
def test_streams_that_cannot_be_reconfigured_are_left_alone(monkeypatch, stream):
    # pytest's capture and shell redirection both replace these; refusing to
    # start because a progress stream is unusual would be far worse.
    monkeypatch.setattr("sys.stdout", stream)
    scripts._force_utf8_console()
