from collections import Counter

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import Episode
from app.services.company_insights import list_company_insights


def build_dashboard() -> dict:
    with SessionLocal() as session:
        episodes = session.scalars(
            select(Episode).order_by(Episode.published_at.desc()).limit(30)
        ).all()
        total_episodes = session.scalar(select(func.count(Episode.id))) or 0

    payload = {
        "mode": "live" if episodes else "empty",
        "total_episodes": total_episodes,
        "latest": [_episode_summary(episode) for episode in episodes],
        "sentiment_summary": dict(Counter(episode.sentiment for episode in episodes if episode.analyzed_at)),
        "companies": list_company_insights(),
    }
    return payload


def episodes_page(page: int = 1, page_size: int = 12) -> dict:
    page = max(1, page)
    page_size = max(1, min(page_size, 50))
    with SessionLocal() as session:
        total = int(session.scalar(select(func.count(Episode.id))) or 0)
        episodes = session.scalars(
            select(Episode)
            .order_by(Episode.published_at.desc(), Episode.episode_number.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": [_episode_detail(episode) for episode in episodes],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "has_previous": page > 1,
        "has_next": page < pages,
    }


def _episode_summary(episode: Episode) -> dict:
    return {
        "episode_number": episode.episode_number,
        "title": episode.title,
        "published_at": episode.published_at.isoformat(),
        "sentiment": episode.sentiment,
        "importance": episode.importance,
        "tickers": [value for value in episode.tickers.split(",") if value],
        "summary": episode.summary,
        "source_url": episode.source_url,
        "transcript_source": episode.transcript_source,
        # Kept on the summary, not just the detail view: a Whisper-filled
        # episode is exactly the one a reader needs flagged while skimming the
        # latest list, since its text came from speech recognition.
        "comparison_score": episode.comparison_score,
        "comparison_notes": episode.comparison_notes,
    }


def _episode_detail(episode: Episode) -> dict:
    return {
        **_episode_summary(episode),
        "description": episode.description,
        "sectors": [value for value in episode.sectors.split(",") if value],
        "analysis_reason": episode.analysis_reason,
        "audio_url": episode.audio_url,
        "vocus_url": episode.vocus_url,
        "transcript_text": episode.transcript_text,
        "transcript_length": len(episode.transcript_text or ""),
        "analyzed_at": episode.analyzed_at.isoformat() if episode.analyzed_at else None,
    }
