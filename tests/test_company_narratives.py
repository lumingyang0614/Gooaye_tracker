"""The company-level narrative pass. Isolated SQLite; the model call is stubbed."""
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

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
    monkeypatch.setattr(insights, "_company_names", lambda: {"2330.TW": "台積電"})
    monkeypatch.setattr(insights, "resolve_market", lambda symbol: "TW")
    db.init_db()
    yield factory
    engine.dispose()


def _seed(factory, summaries=("先進封裝吃緊", "資本支出上修")):
    with factory() as session:
        episode = Episode(
            episode_number=690,
            published_at=datetime.now(timezone.utc),
            title="EP690",
            source_url="https://example.com/690",
            transcript_text="transcript",
        )
        session.add(episode)
        session.flush()
        for index, summary in enumerate(summaries):
            session.add(EpisodeSegment(
                episode_id=episode.id, chunk_index=index, tickers="2330.TW",
                sentiment="bullish", importance=0.8, summary=summary, reason="reason",
            ))
        session.commit()


def _row(factory, symbol="2330.TW") -> CompanyInsight:
    with factory() as session:
        return session.scalar(select(CompanyInsight).where(CompanyInsight.symbol == symbol))


def test_narrative_pass_writes_thesis_and_both_point_lists(database, monkeypatch):
    _seed(database)
    stub = AsyncMock(return_value={"2330.TW": {
        "symbol": "2330.TW",
        "thesis": "CoWoS 產能是今年的關鍵變數。",
        "strengths": ["先進封裝產能吃緊", "資本支出上修"],
        "weaknesses": ["匯率壓力"],
    }})
    monkeypatch.setattr(insights, "analyze_companies", stub)

    result = asyncio.run(insights.rebuild_company_insights(use_llm=True))

    assert result["narratives_attempted"] == 1
    assert result["narratives_completed"] == 1
    row = _row(database)
    assert row.thesis == "CoWoS 產能是今年的關鍵變數。"
    assert json.loads(row.strengths_json) == ["先進封裝產能吃緊", "資本支出上修"]
    assert json.loads(row.weaknesses_json) == ["匯率壓力"]


def test_evidence_block_carries_stance_counts_and_segment_summaries(database, monkeypatch):
    _seed(database)
    stub = AsyncMock(return_value={})
    monkeypatch.setattr(insights, "analyze_companies", stub)

    asyncio.run(insights.rebuild_company_insights(use_llm=True))

    (blocks,), _ = stub.call_args
    evidence = blocks["2330.TW"]
    assert "COMPANY: 台積電" in evidence
    assert "SEGMENT STANCES: bullish 2 / bearish 0 / neutral 0" in evidence
    assert "MENTIONED IN: 1 集 / 2 個片段" in evidence
    assert "先進封裝吃緊" in evidence


def test_model_failure_leaves_the_mechanical_rollup_readable(database, monkeypatch):
    _seed(database)
    monkeypatch.setattr(insights, "analyze_companies", AsyncMock(side_effect=RuntimeError("offline")))

    result = asyncio.run(insights.rebuild_company_insights(use_llm=True))

    assert (result["narratives_attempted"], result["narratives_completed"]) == (1, 0)
    row = _row(database)
    # Falls back to the segment-summary thesis rather than a blank card.
    assert "EP690" in row.thesis
    assert row.stance == "bullish"
    assert json.loads(row.strengths_json) == []


def test_empty_thesis_does_not_overwrite_the_fallback(database, monkeypatch):
    _seed(database)
    monkeypatch.setattr(insights, "analyze_companies", AsyncMock(return_value={"2330.TW": {
        "symbol": "2330.TW", "thesis": "", "strengths": ["先進封裝產能吃緊"], "weaknesses": [],
    }}))

    asyncio.run(insights.rebuild_company_insights(use_llm=True))

    row = _row(database)
    assert "EP690" in row.thesis
    assert json.loads(row.strengths_json) == ["先進封裝產能吃緊"]


def test_default_rebuild_makes_no_model_call(database, monkeypatch):
    _seed(database)
    stub = AsyncMock(return_value={})
    monkeypatch.setattr(insights, "analyze_companies", stub)

    result = asyncio.run(insights.rebuild_company_insights())

    stub.assert_not_awaited()
    assert result["narratives_attempted"] == 0
    assert _row(database).stance == "bullish"


def test_config_switch_disables_the_pass_even_when_asked(database, monkeypatch):
    _seed(database)
    stub = AsyncMock(return_value={})
    monkeypatch.setattr(insights, "analyze_companies", stub)
    monkeypatch.setattr(insights, "load_runtime_config",
                        lambda: {"analysis": {"company_analysis_enabled": False}})

    asyncio.run(insights.rebuild_company_insights(use_llm=True))

    stub.assert_not_awaited()


def test_market_is_stored_and_returned(database, monkeypatch):
    _seed(database)
    asyncio.run(insights.rebuild_company_insights())
    assert _row(database).market == "TW"
    assert insights.list_company_insights()[0]["market"] == "TW"
