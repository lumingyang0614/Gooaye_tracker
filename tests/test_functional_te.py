"""TE acceptance checks. All writes use isolated SQLite; external calls mocked."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from test_episode_segments_qa import database, episode, analysis, analyze
from app import main
from app.models import Episode
from app.services import dashboard, sync, llm, vocus_free


def configure(monkeypatch, pack):
    monkeypatch.setattr(sync, 'load_runtime_config', lambda: {
        'sync': {'incremental_new_limit': 20},
        'sources': {'transcripts_pack': {'pack_url': 'unused', 'episode_url_template': 'unused'},
                    'feed': {'rss_url': 'unused'}, 'whisper': {'enabled': False}}})
    monkeypatch.setattr(sync.transcripts_pack, 'fetch_transcript_pack', AsyncMock(return_value=pack))
    monkeypatch.setattr(sync.feed_service, 'fetch_feed_episodes', AsyncMock(return_value=[]))


def test_failed_analysis_not_counted_as_success(database, monkeypatch):
    configure(monkeypatch, [{'episode_number': 1, 'transcript_text': 'new content'}])
    monkeypatch.setattr(sync, 'analyze_chunks', AsyncMock(side_effect=RuntimeError('offline')))
    result = asyncio.run(sync.sync_gooaye(limit=1))
    assert (result['episodes_analyzed'], result['pending_analysis_backlog']) == (0, 1)


@pytest.mark.parametrize('first_limit, fail_first', [(0, False), (1, True)])
def test_changed_transcript_is_retried_next_sync(database, monkeypatch, first_limit, fail_first):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        analyze(monkeypatch, session, row, [analysis('OLD')])
    configure(monkeypatch, [{'episode_number': 1, 'transcript_text': 'updated transcript'}])
    first = AsyncMock(side_effect=RuntimeError('offline')) if fail_first else AsyncMock(return_value=[analysis('NEW')])
    monkeypatch.setattr(sync, 'analyze_chunks', first)
    asyncio.run(sync.sync_gooaye(limit=first_limit))
    retry = AsyncMock(return_value=[analysis('NEW')])
    monkeypatch.setattr(sync, 'analyze_chunks', retry)
    result = asyncio.run(sync.sync_gooaye(limit=1))
    assert result['episodes_analyzed'] == 1
    assert retry.await_count == 1


def test_first_sync_limit_and_idempotency(database, monkeypatch):
    configure(monkeypatch, [{'episode_number': n, 'transcript_text': f'content {n}'} for n in range(1, 26)])
    mock = AsyncMock(return_value=[analysis('TEST')])
    monkeypatch.setattr(sync, 'analyze_chunks', mock)
    first = asyncio.run(sync.sync_gooaye())
    assert (first['episodes_analyzed'], first['pending_analysis_backlog']) == (20, 5)
    second = asyncio.run(sync.sync_gooaye())
    assert (second['episodes_analyzed'], second['pending_analysis_backlog']) == (5, 0)
    third = asyncio.run(sync.sync_gooaye())
    assert third['episodes_analyzed'] == 0
    with database() as session:
        assert len(session.scalars(select(Episode)).all()) == 25


def test_dashboard_empty_and_pagination(database, monkeypatch):
    monkeypatch.setattr(dashboard, 'SessionLocal', database)
    monkeypatch.setattr(main, 'postgres_health', lambda: True)
    client = TestClient(main.app)
    assert client.get('/api/dashboard').json()['mode'] == 'empty'
    for params in ['page=0', 'page_size=0', 'page_size=51']:
        assert client.get('/api/episodes?' + params).status_code == 400
    assert client.get('/api/episodes?page=abc').status_code == 422
    with database() as session:
        session.add_all([episode(n) for n in range(1, 14)])
        session.commit()
    first = client.get('/api/episodes').json()
    last = client.get('/api/episodes?page=2').json()
    assert len(first['items']) == 12 and first['has_next']
    assert len(last['items']) == 1 and not last['has_next']
    assert {x['episode_number'] for x in first['items']}.isdisjoint({x['episode_number'] for x in last['items']})


def test_dashboard_latest_includes_comparison(database, monkeypatch):
    monkeypatch.setattr(dashboard, 'SessionLocal', database)
    with database() as session:
        session.add(episode(comparison_notes='comparison evidence', comparison_score=0.9))
        session.commit()
    assert dashboard.build_dashboard()['latest'][0].get('comparison_notes') == 'comparison evidence'


def test_fallback_source_matches_actual_text():
    row = episode()
    sync._apply_transcript(row, {'transcript_text': ''}, {'transcript_text': 'free article'}, None)
    assert row.transcript_source == 'vocus_free'


@pytest.mark.parametrize('flag, accepted', [(False, True), (True, False), (None, False), ('missing', False)])
def test_vocus_requires_explicit_free_flag(flag, accepted):
    article = {'title': 'EP700', 'createdAt': '2026-09-01T00:00:00Z', 'content': 'sample'}
    if flag != 'missing':
        article['isPay'] = flag
    html = '<script id="__NEXT_DATA__">' + json.dumps({'props': {'pageProps': {'parsedArticle': article}}}) + '</script>'
    assert (vocus_free._parse_article(html, 'https://example.com') is not None) == accepted


def test_empty_llm_result_is_not_accepted(monkeypatch):
    monkeypatch.setattr(llm, 'chat_json', AsyncMock(return_value={'items': []}))
    with pytest.raises((ValueError, RuntimeError)):
        asyncio.run(llm.analyze_chunks(['material transcript']))


def test_database_offline_returns_explicit_errors(monkeypatch):
    monkeypatch.setattr(main, 'postgres_health', lambda: False)
    client = TestClient(main.app)
    for path in ['/api/dashboard', '/api/episodes', '/api/companies/TEST/segments']:
        assert client.get(path).status_code == 503
    assert client.post('/api/sync').status_code == 503
    assert client.get('/').status_code == 200
