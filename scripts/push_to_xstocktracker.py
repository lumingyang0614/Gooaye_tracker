"""Synthesize Gooaye company narratives, import them, and refresh merged views.

The normal sync run (`python -m scripts.sync_gooaye`) already pushes the
companies it touched, so this script is for the cases that path doesn't cover:
the first backfill after wiring the two projects together, a re-push after the
other database was down, or after editing config/settings.yaml aliases and
rebuilding insights locally.

    python -m scripts.push_to_xstocktracker
    python -m scripts.push_to_xstocktracker --symbols 2330.TW NVDA

Prerequisites, both one-off:
  * `.env` has XSTOCKTRACKER_DB_URL pointing at the X Stock Tracker database.
  * That project has run `python -m scripts.migrate_company_view_source`,
    which adds the `source` column and grants this project's role write access
    to x_company_views.

A full push (no --symbols) also deletes source='gooaye' rows for companies
Gooaye no longer tracks. It never touches source='x' rows.
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.db import init_db
from app.services.company_insights import rebuild_company_insights
from app.services.merged_views import rebuild_merged_views
from app.services.xstock_push import push_company_insights


async def main() -> None:
    parser = argparse.ArgumentParser(description="把 Gooaye 公司觀點推送到 X Stock Tracker")
    parser.add_argument("--symbols", nargs="*", default=None,
                        help="只推送這些代號；省略則全量推送並清掉已不存在的公司")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    init_db()
    analysis = await rebuild_company_insights(selected_symbols=args.symbols, use_llm=True)
    print(f"公司觀點整合：完成 {analysis['narratives_completed']}/{analysis['narratives_attempted']} 家")
    selected = analysis["symbols"] if args.symbols is not None else None
    result = push_company_insights(selected_symbols=selected)
    if result.get("skipped"):
        print(f"已略過推送：{result['skipped']}")
        return
    if result.get("error"):
        raise SystemExit(f"推送失敗：{result['error']}")
    print(f"已推送 {result['pushed']} 家公司到 x_company_views（source=gooaye）"
          f"，刪除 {result['removed']} 筆已失效的資料")
    for symbol in result.get("symbols", []):
        print(f"  {symbol}")
    merged = await rebuild_merged_views(selected_symbols=selected)
    print(f"下游融合結果：{merged}")
    if result.get("pending_narratives"):
        raise SystemExit(f"仍有 {len(result['pending_narratives'])} 家尚未完成整合，未匯入；重跑會重試。")


if __name__ == "__main__":
    asyncio.run(main())
