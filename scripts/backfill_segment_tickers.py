"""Rewrite stored tickers through the current alias list and security masters.

Company grouping already resolves aliases at read time (see
app/services/company_insights.py), so the 公司觀點 page is correct without this
script. What it fixes is the *stored* strings: an episode analyzed before
`scripts.sync_taiwan_securities` ran keeps 「文曄」 in Episode.tickers, so the
episode card shows a $文曄 chip while the company page shows 3036.TW for the
same mention.

Run it after syncing the security master or after adding aliases to
config/settings.yaml, when you want the two views to agree. Idempotent, and it
calls no LLM — resolution is pure lookup.

    python -m scripts.backfill_segment_tickers
    python -m scripts.backfill_segment_tickers --dry-run
"""
import argparse

from sqlalchemy import select

from app.db import SessionLocal, init_db
from app.models import Episode, EpisodeSegment
from app.services.entity_resolver import canonicalize_symbol_map


def _split(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def backfill(dry_run: bool = False) -> dict[str, int]:
    with SessionLocal() as session:
        segments = list(session.scalars(select(EpisodeSegment)))
        episodes = list(session.scalars(select(Episode)))

        raw = {value for row in (*segments, *episodes) for value in _split(row.tickers)}
        canonical = canonicalize_symbol_map(raw)

        changed = {"segments": 0, "episodes": 0}
        for kind, rows in (("segments", segments), ("episodes", episodes)):
            for row in rows:
                current = _split(row.tickers)
                if not current:
                    continue
                # Resolution can map two stored strings onto one symbol, so
                # dedupe: 「文曄」 and 3036.TW in the same row become one entry.
                resolved = ",".join(sorted({canonical.get(value, value.upper()) for value in current}))
                if resolved != (row.tickers or ""):
                    changed[kind] += 1
                    if not dry_run:
                        row.tickers = resolved
        if not dry_run:
            session.commit()

    return {**changed, "distinct_symbols_seen": len(raw)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill canonical tickers onto stored rows")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會改幾筆，不寫入")
    args = parser.parse_args()

    init_db()
    result = backfill(dry_run=args.dry_run)
    prefix = "（試跑，未寫入）" if args.dry_run else ""
    print(
        f"{prefix}代號回填：分段 {result['segments']} 筆、集數 {result['episodes']} 筆需要更新，"
        f"共掃描 {result['distinct_symbols_seen']} 個不同代號"
    )
    if not args.dry_run and (result["segments"] or result["episodes"]):
        print("建議接著執行：python -m scripts.rebuild_company_insights")


if __name__ == "__main__":
    main()
