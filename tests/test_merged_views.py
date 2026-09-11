"""Fusing both trackers' views of a company into one.

The point of this pass is integration rather than juxtaposition, so the things
worth pinning are: that only companies with genuinely two views cost a model
call, that a failed call still leaves a readable row, and that neither
tracker's own rows are ever touched.
"""
import asyncio
import json

import pytest

from app.services import merged_views
from app.services.merged_views import (
    SOURCE_GOOAYE,
    SOURCE_MERGED,
    SOURCE_X,
    _combined_evidence,
    _display_name,
    _mechanical_row,
    rebuild_merged_views,
)
from app.services.llm import MERGED_INSTRUCTIONS, _normalize_merged


def _row(source, symbol="MU", stance="bullish", thesis="論點", name="Micron",
         strengths='["優勢一"]', weaknesses='["風險一"]', urls='["https://example.test/a"]', mentions=1):
    return {
        "source": source, "symbol": symbol, "company_name": name, "stance": stance,
        "confidence": 0.6, "thesis": thesis, "strengths_json": strengths,
        "weaknesses_json": weaknesses, "evidence_urls_json": urls, "mention_count": mentions,
    }


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params))
        if sql.startswith("SELECT"):
            return [type("R", (), {"_mapping": row})() for row in self.rows]
        return type("R", (), {"rowcount": 0})()

    def __enter__(self): return self
    def __exit__(self, *exc): return False


class FakeEngine:
    def __init__(self, rows):
        self.connection = FakeConnection(rows)

    def connect(self): return self.connection
    def begin(self): return self.connection


@pytest.fixture
def wired(monkeypatch):
    def _wire(rows, synthesis=None, config=None):
        engine = FakeEngine(rows)
        monkeypatch.setattr(merged_views, "xstocktracker_engine", lambda: engine)
        monkeypatch.setattr(merged_views, "load_runtime_config",
                            lambda: {"xstocktracker": config or {}})
        calls = []

        async def fake_synth(blocks, max_tokens=8192):
            calls.append(sorted(blocks))
            if synthesis == "fail":
                raise RuntimeError("provider down")
            return {symbol: {"symbol": symbol, "stance": "mixed", "thesis": "融合論點",
                             "strengths": ["合併優勢"], "weaknesses": ["合併風險"]}
                    for symbol in blocks}

        monkeypatch.setattr(merged_views, "synthesize_merged_views", fake_synth)
        return engine, calls
    return _wire


# --- the prompt contract -----------------------------------------------------

def test_prompt_forbids_listing_the_sources_separately():
    """The whole request: one opinion, not two labelled blocks."""
    assert "融合成一份觀點" in MERGED_INSTRUCTIONS
    assert "嚴禁" in MERGED_INSTRUCTIONS and "並列" in MERGED_INSTRUCTIONS


def test_prompt_does_not_ask_the_model_to_reweight_sources():
    """Both inputs are already weighted aggregates — X Stock Tracker applies
    its Serenity playbook bonus upstream. Re-weighting here would double-count."""
    assert "不需要再自行加權任何來源" in MERGED_INSTRUCTIONS


@pytest.mark.parametrize("stance,expected", [
    ("bullish", "bullish"), ("BEARISH", "bearish"), ("mixed", "mixed"),
    ("很看好", "neutral"), ("", "neutral"), (None, "neutral"),
])
def test_unexpected_stance_values_fall_back_to_neutral(stance, expected):
    assert _normalize_merged({"symbol": "MU", "stance": stance})["stance"] == expected


# --- building the evidence block --------------------------------------------

def test_evidence_block_carries_both_views_and_unions_the_links():
    evidence, urls = _combined_evidence({
        SOURCE_GOOAYE: _row(SOURCE_GOOAYE, thesis="股癌論點", urls='["https://a"]'),
        SOURCE_X: _row(SOURCE_X, thesis="X 論點", urls='["https://b", "https://a"]'),
    })
    assert "股癌論點" in evidence and "X 論點" in evidence
    assert urls == ["https://a", "https://b"]  # de-duplicated, order kept


def test_display_name_prefers_a_real_name_over_a_repeated_ticker():
    sources = {SOURCE_X: _row(SOURCE_X, name="MU"), SOURCE_GOOAYE: _row(SOURCE_GOOAYE, name="Micron")}
    assert _display_name(sources, "MU") == "Micron"


def test_display_name_falls_back_to_the_symbol():
    assert _display_name({SOURCE_X: _row(SOURCE_X, name="")}, "MU") == "MU"


# --- the mechanical row ------------------------------------------------------

def test_agreeing_sources_keep_the_shared_stance():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, stance="bullish"),
                                 SOURCE_X: _row(SOURCE_X, stance="bullish")}, None)
    assert row["stance"] == "bullish" and row["source"] == SOURCE_MERGED


def test_disagreeing_sources_are_marked_mixed_not_silently_picked():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, stance="bullish"),
                                 SOURCE_X: _row(SOURCE_X, stance="bearish")}, None)
    assert row["stance"] == "mixed"


def test_mention_counts_are_summed_across_sources():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, mentions=3),
                                 SOURCE_X: _row(SOURCE_X, mentions=4)}, None)
    assert row["mention_count"] == 7


def test_points_from_both_sources_are_merged_and_deduplicated():
    row = _mechanical_row("MU", {
        SOURCE_GOOAYE: _row(SOURCE_GOOAYE, strengths='["共同優勢", "股癌獨有"]'),
        SOURCE_X: _row(SOURCE_X, strengths='["共同優勢", "X 獨有"]'),
    }, None)
    assert json.loads(row["strengths_json"]) == ["共同優勢", "股癌獨有", "X 獨有"]


# --- what actually costs a model call ---------------------------------------

def test_only_companies_with_two_views_are_sent_to_the_model(wired):
    _, calls = wired([_row(SOURCE_GOOAYE, symbol="MU"), _row(SOURCE_X, symbol="MU"),
                      _row(SOURCE_GOOAYE, symbol="SOLO")])
    result = asyncio.run(rebuild_merged_views())
    assert calls == [["MU"]], "a single-source company has nothing to fuse"
    assert result["merged"] == 2 and result["two_sided"] == 1


def test_single_source_companies_still_get_a_merged_row(wired):
    engine, _ = wired([_row(SOURCE_GOOAYE, symbol="SOLO")])
    asyncio.run(rebuild_merged_views())
    written = next(p for sql, p in engine.connection.executed if sql.startswith("INSERT"))
    assert [row["symbol"] for row in written] == ["SOLO"]
    assert written[0]["source"] == SOURCE_MERGED


def test_synthesis_result_replaces_the_mechanical_text(wired):
    engine, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)])
    asyncio.run(rebuild_merged_views())
    written = next(p for sql, p in engine.connection.executed if sql.startswith("INSERT"))[0]
    assert written["thesis"] == "融合論點" and written["stance"] == "mixed"
    assert json.loads(written["strengths_json"]) == ["合併優勢"]


def test_a_failed_model_call_still_leaves_a_readable_row(wired):
    """Degrading to the mechanical rollup beats showing a blank company."""
    engine, _ = wired([_row(SOURCE_GOOAYE, thesis="股癌論點"), _row(SOURCE_X)], synthesis="fail")
    result = asyncio.run(rebuild_merged_views())
    written = next(p for sql, p in engine.connection.executed if sql.startswith("INSERT"))[0]
    assert written["thesis"] == "股癌論點"
    assert result["synthesized"] == 0
    assert result["failures"][0]["error"] == "RuntimeError"


def test_llm_can_be_switched_off(wired):
    _, calls = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)], config={"merge_llm_enabled": False})
    asyncio.run(rebuild_merged_views())
    assert calls == []


def test_use_llm_false_skips_synthesis(wired):
    _, calls = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)])
    asyncio.run(rebuild_merged_views(use_llm=False))
    assert calls == []


def test_selected_symbols_limits_the_work(wired):
    _, calls = wired([_row(SOURCE_GOOAYE, symbol="MU"), _row(SOURCE_X, symbol="MU"),
                      _row(SOURCE_GOOAYE, symbol="NVDA"), _row(SOURCE_X, symbol="NVDA")])
    result = asyncio.run(rebuild_merged_views(selected_symbols=["mu"]))
    assert calls == [["MU"]] and result["merged"] == 1


def test_no_database_configured_is_a_skip_not_a_failure(monkeypatch):
    monkeypatch.setattr(merged_views, "xstocktracker_engine", lambda: None)
    assert asyncio.run(rebuild_merged_views())["skipped"] == "XSTOCKTRACKER_DB_URL 未設定"


def test_existing_merged_rows_are_never_read_back_as_input(wired):
    """Feeding a previous fusion back in would compound its own summary."""
    engine, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)])
    asyncio.run(rebuild_merged_views())
    select_sql, params = next((s, p) for s, p in engine.connection.executed if s.startswith("SELECT"))
    assert "source <> :merged" in select_sql and params["merged"] == SOURCE_MERGED


def test_only_merged_rows_are_written(wired):
    """Neither tracker's own rows may be modified by this pass."""
    engine, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)])
    asyncio.run(rebuild_merged_views())
    for sql, params in engine.connection.executed:
        if sql.startswith(("INSERT", "DELETE")):
            values = params if isinstance(params, list) else [params]
            assert all(v.get("source", v.get("merged")) == SOURCE_MERGED for v in values), sql
