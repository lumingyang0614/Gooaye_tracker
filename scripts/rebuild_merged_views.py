"""Fuse both trackers' views of each company into one merged row.

`x_company_views` holds one row per source, so a consumer that looks a company
up by name gets two answers for anything both trackers cover. This writes a
third row per company, `source='merged'`, holding one integrated opinion — not
the two texts pasted together, but a single view produced from both.

    python -m scripts.rebuild_merged_views
    python -m scripts.rebuild_merged_views --symbols MU NVDA
    python -m scripts.rebuild_merged_views --no-llm
    python -m scripts.rebuild_merged_views --force

Only companies both trackers actually cover cost a model call, and only when a
source row is newer than the merged row already there; a company just one side
covers is copied through mechanically. `--force` re-fuses even unchanged
companies, which is the expensive option and rarely what you want.

`--no-llm` skips synthesis: cheap, but for a company that has two views it
replaces a previously fused thesis with the mechanical rollup, so it is for
populating rows rather than for routine refreshes.

Run `python -m scripts.create_unified_view` once beforehand so consumers have
the view that reads these rows.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.services.merged_views import rebuild_merged_views


async def main() -> None:
    parser = argparse.ArgumentParser(description="用 LLM 把兩邊的公司觀點融合成一份")
    parser.add_argument("--symbols", nargs="*", default=None, help="只處理這些代號")
    parser.add_argument("--no-llm", action="store_true", help="只做機械彙整，不呼叫模型")
    parser.add_argument("--force", action="store_true", help="連沒變動的公司也重做")
    args = parser.parse_args()

    # The standalone CLI does not inherit uvicorn's logging configuration.
    # StreamHandler flushes each record, including when output is redirected.
    progress_logger = logging.getLogger("uvicorn.error")
    previous_level = progress_logger.level
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
    progress_logger.addHandler(handler)
    progress_logger.setLevel(logging.INFO)

    try:
        result = await rebuild_merged_views(
            selected_symbols=args.symbols, use_llm=not args.no_llm, force=args.force)
    finally:
        progress_logger.removeHandler(handler)
        progress_logger.setLevel(previous_level)
        handler.close()
    if result.get("skipped"):
        print(f"已略過：{result['skipped']}")
        return

    print("合併觀點完成")
    print(f"  重寫的公司      : {result['merged']}")
    print(f"  未變動而跳過    : {result['unchanged']}")
    print(f"  兩邊都有的公司  : {result['two_sided']}")
    print(f"  LLM 融合成功    : {result['synthesized']}")
    print(f"  清掉的失效列    : {result['removed']}")
    if result["not_fused"]:
        reason = "（--no-llm，這些公司的融合論點已換成機械彙整）" if args.no_llm else "（維持機械彙整）"
        print(f"  未融合          : {result['not_fused']} {reason}")
    for failure in result.get("failures", []):
        print(f"  未完成          : {','.join(failure['symbols'])} ({failure['error']})")


if __name__ == "__main__":
    asyncio.run(main())
