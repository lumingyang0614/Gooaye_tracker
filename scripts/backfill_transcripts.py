"""One-off historical backfill: analyze every episode in the transcript pack,
not just the newest `sync.incremental_new_limit` from config/settings.yaml.

This calls the LLM once per pending episode (roughly 700 calls for the full
archive), so it can take a long time — run it deliberately, not on a schedule.
"""
import asyncio

from app.db import init_db
from app.services.sync import sync_gooaye


async def main() -> None:
    init_db()
    result = await sync_gooaye(limit=10_000)
    print(result)


if __name__ == "__main__":
    asyncio.run(main())
