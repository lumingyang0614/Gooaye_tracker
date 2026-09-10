import asyncio
import json
from unittest.mock import AsyncMock

import pytest
import httpx
from sqlalchemy import select
from app.models import Episode, EpisodeSegment, SegmentCompanyStance
from app.services import llm, entity_resolver, company_insights as insights
from test_episode_segments_qa import database, episode, analyze, analysis


def test_unverified_platform_is_not_classified_as_us(monkeypatch):
    monkeypatch.setattr(entity_resolver, 'load_taiwan_security_lookup', lambda: {})
    monkeypatch.setattr(entity_resolver, 'load_security_lookup', lambda: {})
    assert entity_resolver.resolve_market('STEAM') == 'UNKNOWN'
    assert entity_resolver.resolve_market('NVDA') == 'US'


def test_company_contract_rejects_wrong_wrapper(monkeypatch):
    monkeypatch.setattr(llm, 'chat_json', AsyncMock(return_value={'items': []}))
    with pytest.raises(ValueError):
        asyncio.run(llm.analyze_companies({'NVDA': 'evidence'}))


def test_company_contract_keeps_valid_empty_points(monkeypatch):
    mock = AsyncMock(return_value={'companies': [{'symbol': 'NVDA', 'thesis': 'mentioned only',
                                                 'strengths': [], 'weaknesses': []}]})
    monkeypatch.setattr(llm, 'chat_json', mock)
    result = asyncio.run(llm.analyze_companies({'NVDA': 'evidence'}))
    assert result['NVDA']['strengths'] == []
    assert '"companies"' in mock.call_args.args[0][1]['content']


def test_company_stance_is_not_inherited_from_other_ticker(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        item = analysis('2330.TW', 'bullish', 0.8)
        item['tickers'] = ['2330.TW', 'NVDA']
        item['mentions'] = [
            {'symbol': '2330.TW', 'sentiment': 'neutral', 'reason': 'only an example'},
            {'symbol': 'NVDA', 'sentiment': 'bullish', 'reason': 'strong demand'},
        ]
        analyze(monkeypatch, session, row, [item])
        asyncio.run(insights.rebuild_company_insights())
        companies = {c['symbol']: c for c in insights.list_company_insights()}
        assert companies['2330.TW']['stance'] == 'neutral'
        assert companies['NVDA']['stance'] == 'bullish'
        assert companies['2330.TW']['confidence'] == 0.8
        assert insights.list_symbol_segments('2330.TW')[0]['reason'] == 'only an example'
        # Reanalysis must replace the child votes as well as the segments.
        analyze(monkeypatch, session, row, [item])
        assert len(session.scalars(select(SegmentCompanyStance)).all()) == 2


def test_missing_individual_vote_is_neutral(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        item = analysis('NVDA', 'bullish')
        item['mentions'] = []
        analyze(monkeypatch, session, row, [item])
    asyncio.run(insights.rebuild_company_insights())
    assert insights.list_company_insights()[0]['stance'] == 'neutral'


def test_stance_tie_is_neutral(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        analyze(monkeypatch, session, row, [analysis('NVDA', 'bullish'), analysis('NVDA', 'neutral')])
    asyncio.run(insights.rebuild_company_insights())
    assert insights.list_company_insights()[0]['stance'] == 'neutral'


def test_bai_receives_schema_and_retries_wrong_json_shape(monkeypatch):
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        content = {'items': []} if len(requests) == 1 else {'companies': [
            {'symbol': 'NVDA', 'thesis': 'test', 'strengths': [], 'weaknesses': []}]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(content)}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond)))
    result = asyncio.run(llm._chat_openai_compatible(
        api_url='https://fixture.invalid', api_key='fixture', model='fixture',
        messages=[{'role': 'user', 'content': 'test'}], schema=llm.COMPANY_SCHEMA,
        max_tokens=1000, timeout=10, use_json_schema=False))
    assert result['companies'][0]['symbol'] == 'NVDA'
    assert len(requests) == 2
    assert 'response_format' not in requests[0]
    assert json.dumps(llm.COMPANY_SCHEMA, ensure_ascii=False) in requests[0]['messages'][-1]['content']


def test_provider_rejection_reports_code_and_keeps_fallback(database, monkeypatch):
    with database() as session:
        row = episode()
        session.add(row)
        session.commit()
        analyze(monkeypatch, session, row, [analysis('NVDA')])
    response = httpx.Response(400, request=httpx.Request('POST', 'https://fixture.invalid'),
                             json={'error': {'code': '1301', 'message': 'provider refusal'}})
    error = httpx.HTTPStatusError('rejected', request=response.request, response=response)
    monkeypatch.setattr(insights, 'analyze_companies', AsyncMock(side_effect=error))
    result = asyncio.run(insights.rebuild_company_insights(use_llm=True))
    assert result['narratives_completed'] == 0
    assert result['narrative_failures'] == [{'symbols': ['NVDA'], 'error': 'HTTPStatusError',
                                            'status': 400, 'provider_code': '1301'}]
    assert insights.list_company_insights()[0]['thesis']
