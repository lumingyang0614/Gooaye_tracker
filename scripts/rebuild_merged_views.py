"""Fuse both trackers' views of each company into one merged row.

`x_company_views` holds one row per source, so a consumer that looks a company
up by name gets two answers for anything both trackers cover. This writes a
third row per company, `source='merged'`, holding one integrated opinion — not
the two texts pasted together, but a single view produced from both.

    python -m scripts.rebuild_merged_views
    python -m scripts.rebuild_merged_views --symbols MU NVDA
    python -m scripts.rebuild_merged_views --no-llm

Only companies both trackers actually cover cost a model call; a company just
one side covers is copied through mechanically. `--no-llm` skips synthesis
entirely and only refreshes the mechanical rollup, which is the cheap way to
make sure every company has a merged row.

Run `python -m scripts.create_unified_view` once beforehand so consumers have
the view that reads these rows.
"""
from __future__ import annotations

import argparse
import asyncio

from app.services.merged_views import rebuild_merged_views


async def main() -> None:
    parser = argparse.ArgumentParser(description="用 LLM 把兩邊的公司觀點融合成一份")
    parser.add_argument("--symbols", nargs="*", default=None, help="只處理這些代號")
    parser.add_argument("--no-llm", action="store_true", help="只做機械彙整，不呼叫模型")
    args = parser.parse_args()

    result = await rebuild_merged_views(selected_symbols=args.symbols, use_llm=not args.no_llm)
    if result.get("skipped"):
        print(f"已略過：{result['skipped']}")
        return

    print(f"合併觀點完成")
    print(f"  公司數          : {result['merged']}")
    print(f"  兩邊都有的公司  : {result['two_sided']}")
    print(f"  LLM 融合成功    : {result['synthesized']}")
    print(f"  清掉的失效列    : {result['removed']}")
    for failure in result.get("failures", []):
        print(f"  未完成          : {','.join(failure['symbols'])} ({failure['error']})")


if __name__ == "__main__":
    asyncio.run(main())
