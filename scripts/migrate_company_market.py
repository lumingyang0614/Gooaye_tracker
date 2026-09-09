"""Add company_insights.market to a database created before that column existed.

`init_db()` only runs CREATE TABLE IF NOT EXISTS, so it silently leaves an
already-created table without the new column and every later query fails with
UndefinedColumn. This adds the column in place and fills it in per row.

Deliberately additive: it writes nothing but `market`. An earlier version
called rebuild_company_insights() to do the backfill, which was wrong for a
migration — that rebuild deletes companies with no EpisodeSegment rows (which
is every company in a database predating the segments table), and even where
segments do exist it overwrites thesis and clears strengths/weaknesses. A
migration must never destroy the analysis it is migrating.

Run `python -m scripts.rebuild_company_insights` afterwards when you do want
the rows re-derived; that is the command whose job it is.

Safe to re-run: the ALTER is guarded and the backfill is idempotent.
"""
import asyncio

from sqlalchemy import select, text

from app.db import SessionLocal, engine, init_db
from app.models import CompanyInsight
from app.services.entity_resolver import resolve_market


def add_column() -> bool:
    with engine.begin() as connection:
        exists = connection.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'company_insights' AND column_name = 'market'"
        )).first()
        if exists:
            return False
        connection.execute(text(
            "ALTER TABLE company_insights ADD COLUMN market VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN'"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_company_insights_market ON company_insights (market)"
        ))
    return True


def backfill_markets() -> int:
    """Set market on every company row, touching no other column."""
    updated = 0
    with SessionLocal() as session:
        for row in session.scalars(select(CompanyInsight)):
            market = resolve_market(row.symbol)
            if row.market != market:
                row.market = market
                updated += 1
        session.commit()
    return updated


async def main() -> None:
    init_db()
    added = add_column()
    print("已新增 company_insights.market 欄位" if added else "company_insights.market 已存在，略過")

    updated = backfill_markets()
    print(f"已回填 {updated} 家公司的市場分類（既有論點與優劣勢未更動）")


if __name__ == "__main__":
    asyncio.run(main())
