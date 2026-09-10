from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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


class EpisodeSegment(Base):
    """One analyzed chunk of an episode's transcript (see app/services/chunking.py).

    Persisted *before* sync.py merges per-chunk results into the parent
    Episode's episode-level sentiment/tickers, so a stock mentioned bearishly
    in one segment isn't lost or overwritten by the episode's overall
    (majority-vote) stance.
    """
    __tablename__ = "episode_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id", ondelete="CASCADE"), index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)

    sentiment: Mapped[str] = mapped_column(String(16), default="neutral")
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    tickers: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    episode: Mapped["Episode"] = relationship()
    company_stances: Mapped[list["SegmentCompanyStance"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class SegmentCompanyStance(Base):
    """Company-specific votes; a new table keeps existing segment schemas readable."""
    __tablename__ = "segment_company_stances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    segment_id: Mapped[int] = mapped_column(ForeignKey("episode_segments.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(200), index=True)
    sentiment: Mapped[str] = mapped_column(String(16), default="neutral")
    reason: Mapped[str] = mapped_column(Text, default="")


class CompanyInsight(Base):
    """One row per company mentioned across all analyzed episodes.

    `market` is resolved deterministically from the symbol
    (app/services/entity_resolver.py's resolve_market), never asked of the
    LLM — a model guessing "is 2802 Taiwanese or Japanese?" is precisely how
    a Taiwan-focused board fills up with foreign numeric codes. Values match
    the Serenity Stock Tracker project's own market enum (TW/US/OTHER/UNKNOWN)
    so the two projects' company data stays comparable.
    """

    __tablename__ = "company_insights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    company_name: Mapped[str] = mapped_column(String(200), default="")
    market: Mapped[str] = mapped_column(String(16), default="UNKNOWN", index=True)
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


class TaiwanSecurity(Base):
    """TWSE (上市) + TPEx (上櫃) security master, synced from the exchanges'
    own OpenAPI endpoints by app/services/taiwan_securities.py.

    Replaces guessing at 4-digit codes with the real listing table, so 股癌
    mentioning 文曄 or 京元電 resolves to 3036.TW / 2449.TW instead of being
    stored as a raw Chinese string that never joins up with anything.

    canonical_symbol is always `<code>.TW` (the identity used everywhere else
    in this project); provider_symbol keeps the quote-vendor spelling, where
    上櫃 companies are `.TWO` — kept now so a future quote sync doesn't have
    to re-derive it.
    """

    __tablename__ = "taiwan_securities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    canonical_symbol: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    provider_symbol: Mapped[str] = mapped_column(String(24), default="")
    company_name: Mapped[str] = mapped_column(String(200), index=True)
    english_name: Mapped[str] = mapped_column(String(200), default="")
    exchange: Mapped[str] = mapped_column(String(16), index=True)
    industry: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(120), default="")
    is_active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
