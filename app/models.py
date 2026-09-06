from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Episode(Base):
    """One 股癌 (Gooaye) podcast episode: transcript, provenance, and derived analysis.

    transcript_source is one of "whatmkreallysaid" (non-official AI transcript site),
    "vocus_free" (vocus.cc free preview), or "whisper" (local speech-to-text fallback
    used to fill the gap before an episode's written transcript is published).
    """
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    episode_number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    source_url: Mapped[str] = mapped_column(String(500), default="")
    audio_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    vocus_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    transcript_text: Mapped[str] = mapped_column(Text, default="")
    transcript_source: Mapped[str] = mapped_column(String(32), default="unknown")
    whisper_transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    comparison_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    comparison_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    summary: Mapped[str] = mapped_column(Text, default="")
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    sectors: Mapped[str] = mapped_column(Text, default="")
    tickers: Mapped[str] = mapped_column(Text, default="")
    analysis_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )


class CompanyInsight(Base):
    __tablename__ = "company_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(200), default="")
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    stance: Mapped[str] = mapped_column(String(16), default="neutral")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    thesis: Mapped[str] = mapped_column(Text, default="")
    strengths_json: Mapped[str] = mapped_column(Text, default="[]")
    weaknesses_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence_urls_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
