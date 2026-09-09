"""Sync the official TWSE (上市) + TPEx (上櫃) security master.

Run this once before the first sync, then whenever you want to pick up new
listings. It replaces the hand-maintained seed list in config/settings.yaml as
the bulk source of Taiwan company-name -> ticker knowledge, so 股癌 mentioning
文曄 or 京元電 resolves to a real symbol instead of being stored as a raw
string. Needs network access but no API key.

Existing company insights are not re-derived automatically — follow up with
`python -m scripts.rebuild_company_insights --no-llm` if you want previously
unresolved mentions folded in without paying for another analysis pass.
"""
import asyncio

from app.db import init_db
from app.services.taiwan_securities import sync_taiwan_securities


async def main() -> None:
    init_db()
    result = await sync_taiwan_securities()
    print(f"台股證券主檔：共 {result['total']} 家（上市 {result['twse']}／上櫃 {result['tpex']}）")


if __name__ == "__main__":
    asyncio.run(main())
