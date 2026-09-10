"""Reanalyze explicitly selected stored transcripts, including company votes."""
import argparse
import asyncio

from sqlalchemy import select
from app.db import SessionLocal, init_db
from app.models import Episode
from app.runtime_config import load_runtime_config
from app.services.sync import _analyze_episode
from app.services.company_insights import rebuild_company_insights


async def main():
    parser = argparse.ArgumentParser(description='重新分析指定集數的既有逐字稿（呼叫 LLM）')
    parser.add_argument('--episodes', type=int, nargs='+', required=True)
    args = parser.parse_args()
    if any(n < 1 for n in args.episodes):
        parser.error('episode numbers must be positive')
    init_db()
    affected = set()
    failures = []
    for number in sorted(set(args.episodes)):
        with SessionLocal() as session:
            episode = session.scalar(select(Episode).where(Episode.episode_number == number))
            if episode is None or not episode.transcript_text:
                failures.append(number)
                continue
            previous = episode.analyzed_at
            symbols = await _analyze_episode(session, episode, load_runtime_config().get('sync', {}))
            if episode.analyzed_at is None or episode.analyzed_at == previous:
                failures.append(number)
                continue
            session.commit()
            affected.update(symbols)
    result = await rebuild_company_insights(selected_symbols=sorted(affected), use_llm=True)
    print({'failed_episodes': failures, 'company_insights': result})
    if failures or result['narratives_completed'] != result['narratives_attempted']:
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
