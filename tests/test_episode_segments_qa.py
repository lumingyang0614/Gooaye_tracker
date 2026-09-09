"""Isolated integration coverage; no production DB or LLM calls."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import db, main
from app.models import Base, CompanyInsight, Episode, EpisodeSegment
from app.services import company_insights as insights, sync


@pytest.fixture
def database(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool,
                           connect_args={'check_same_thread': False})
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, 'engine', engine)
    monkeypatch.setattr(sync, 'SessionLocal', factory)
    monkeypatch.setattr(insights, 'SessionLocal', factory)
    monkeypatch.setattr(insights, '_company_names', lambda: {})
    # sync_gooaye now asks for company narratives; stub the model call so this
    # module keeps making no network requests. Returning {} exercises the
    # degrade-to-mechanical path, leaving every assertion below unchanged.
    monkeypatch.setattr(insights, 'analyze_companies', AsyncMock(return_value={}))
    monkeypatch.setattr(sync, 'canonicalize_symbols',
                        lambda values: {'2330.TW' if v == '台積電' else v for v in values})
    db.init_db()
    yield factory
    engine.dispose()


def episode(number=1, **kwargs):
    return Episode(episode_number=number, published_at=datetime.now(timezone.utc),
                   title=f'EP{number}', source_url=f'https://example.com/{number}',
                   transcript_text='unchanged transcript', **kwargs)


def analysis(ticker, stance='bullish', importance=0.8):
    return dict(tickers=[ticker], sentiment=stance, importance=importance,
                summary_zh_tw=f'{ticker} {stance}', reason='reason', sectors=[])


def analyze(monkeypatch, session, row, items):
    monkeypatch.setattr(sync, 'analyze_chunks', AsyncMock(return_value=items))
    result = asyncio.run(sync._analyze_episode(session, row, {}))
    session.commit()
    return result


def test_init_db_adds_table_to_existing_schema(database):
    EpisodeSegment.__table__.drop(db.engine)
    with database() as session:
        session.add(episode())
        session.commit()
    db.init_db()
    db.init_db()
    assert 'episode_segments' in inspect(db.engine).get_table_names()
    with database() as session:
        assert session.scalar(select(Episode)).episode_number == 1


def test_persistence_rollup_and_replacement(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        items = [analysis('台積電'), analysis('台積電'), analysis('RARE', 'bearish', 0.2)]
        analyze(monkeypatch, session, row, items)
        assert row.sentiment == 'bullish'
        segments = session.scalars(select(EpisodeSegment).order_by(EpisodeSegment.chunk_index)).all()
        assert [(s.tickers, s.sentiment) for s in segments] == [
            ('2330.TW', 'bullish'), ('2330.TW', 'bullish'), ('RARE', 'bearish')]
        asyncio.run(insights.rebuild_company_insights())
        rare = next(x for x in insights.list_company_insights() if x['symbol'] == 'RARE')
        assert rare['stance'] == 'bearish'
        assert rare['confidence'] == pytest.approx(0.2)
        assert rare['mention_count'] == 1
        assert 'RARE bearish' in rare['thesis']
        analyze(monkeypatch, session, row, items[:1])
    assert len(insights.list_symbol_segments('2330.TW')) == 1
    assert insights.list_symbol_segments('RARE') == []


def test_api_sort_fields_normalization_empty_and_unavailable(database, monkeypatch):
    with database() as session:
        for number in [1, 3, 2]:
            row = episode(number)
            session.add(row)
            session.flush()
            session.add(EpisodeSegment(episode_id=row.id, chunk_index=0, tickers='RARE',
                                       sentiment='bearish', importance=0.2,
                                       summary='summary', reason='reason'))
        session.commit()
    monkeypatch.setattr(main, 'postgres_health', lambda: True)
    client = TestClient(main.app)
    response = client.get('/api/companies/rare/segments')
    assert response.status_code == 200
    data = response.json()
    assert data['symbol'] == 'RARE'
    assert [s['episode_number'] for s in data['segments']] == [3, 2, 1]
    assert data['segments'][0] == dict(episode_number=3, episode_title='EP3',
        chunk_index=0, sentiment='bearish', importance=0.2, summary='summary',
        reason='reason', source_url='https://example.com/3',
        published_at=data['segments'][0]['published_at'])
    assert data['segments'][0]['published_at']
    assert client.get('/api/companies/UNKNOWN/segments').json()['segments'] == []
    monkeypatch.setattr(main, 'postgres_health', lambda: False)
    assert client.get('/api/companies/RARE/segments').status_code == 503


def test_failed_llm_preserves_existing_segments(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        analyze(monkeypatch, session, row, [analysis('RARE')])
        monkeypatch.setattr(sync, 'analyze_chunks', AsyncMock(side_effect=RuntimeError('offline')))
        assert asyncio.run(sync._analyze_episode(session, row, {})) == set()
        session.commit()
    assert len(insights.list_symbol_segments('RARE')) == 1


def test_removed_symbol_does_not_leave_stale_insight(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        analyze(monkeypatch, session, row, [analysis('RARE')])
        asyncio.run(insights.rebuild_company_insights())
        affected = analyze(monkeypatch, session, row, [analysis('NEW')])
    asyncio.run(insights.rebuild_company_insights(selected_symbols=sorted(affected)))
    assert insights.list_symbol_segments('RARE') == []
    assert 'RARE' not in {x['symbol'] for x in insights.list_company_insights()}


def test_full_rebuild_removes_orphan_and_retains_supported_insight(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.add(CompanyInsight(symbol='ORPHAN'))
        session.commit()
        analyze(monkeypatch, session, row, [analysis('KEEP', importance=0)])
    result = asyncio.run(insights.rebuild_company_insights())
    assert result['symbols_removed'] == 1
    remaining = insights.list_company_insights()
    assert [item['symbol'] for item in remaining] == ['KEEP']
    assert remaining[0]['confidence'] == 0


@pytest.mark.parametrize('limit, expected', [(1, 1), (3, 3), (None, 2)])
def test_sync_backfills_previously_analyzed_episode(database, monkeypatch, limit, expected):
    with database() as session:
        for number in range(1, 4):
            session.add(episode(number, analyzed_at=datetime.now(timezone.utc), tickers='RARE'))
        session.commit()
    monkeypatch.setattr(sync, 'load_runtime_config', lambda: {
        'sync': {'incremental_new_limit': 2},
        'sources': {'transcripts_pack': {'pack_url': 'unused', 'episode_url_template': 'unused'},
                    'feed': {'rss_url': 'unused'}, 'whisper': {'enabled': False}}})
    monkeypatch.setattr(sync.transcripts_pack, 'fetch_transcript_pack', AsyncMock(return_value=[
        {'episode_number': number, 'transcript_text': 'unchanged transcript'} for number in range(1, 4)]))
    monkeypatch.setattr(sync.feed_service, 'fetch_feed_episodes', AsyncMock(return_value=[]))
    monkeypatch.setattr(sync, 'analyze_chunks', AsyncMock(return_value=[analysis('RARE')]))
    result = asyncio.run(sync.sync_gooaye(limit=limit))
    assert result['episodes_analyzed'] == expected
    assert result['pending_analysis_backlog'] == 3 - expected
    assert len(insights.list_symbol_segments('RARE')) == expected
    result = asyncio.run(sync.sync_gooaye(limit=3))
    assert result['episodes_analyzed'] == 3 - expected
    assert result['pending_analysis_backlog'] == 0
    assert len(insights.list_symbol_segments('RARE')) == 3
