"""Re-derive company insights from the episode segments already in the database.

Runs no transcript sync and no episode analysis, so it is far cheaper than a
full backfill. Use it after:

- editing `company_aliases` in config/settings.yaml,
- running `python -m scripts.sync_taiwan_securities`,
- upgrading from a version whose company rows had no thesis or優劣勢.

By default this calls the LLM once per `analysis.company_analysis_batch_size`
companies to write each company's thesis and strengths/weaknesses. Pass
--no-llm to rebuild only the mechanical parts (stance, 重要度, market, mention
count, evidence links) with no model calls at all.
"""
import argparse
import asyncio

from app.db import init_db
from app.services.company_insights import rebuild_company_insights


async def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild Gooaye company insights")
    parser.add_argument(
        "--symbols",
        nargs="*",
        help="只重建這些代號（預設全部）；例如 --symbols 2330.TW NVDA",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="跳過公司級論點分析，不呼叫任何模型",
    )
    args = parser.parse_args()

    init_db()
    result = await rebuild_company_insights(
        selected_symbols=args.symbols or None,
        use_llm=not args.no_llm,
    )
    print(
        f"公司觀點重建完成：更新 {result['symbols_updated']} 家、"
        f"清除 {result['symbols_removed']} 家、"
        f"論點 {result['narratives_completed']}/{result['narratives_attempted']} 家"
    )


if __name__ == "__main__":
    asyncio.run(main())
