"""Fusing both trackers' views of a company into one.

The point of this pass is integration rather than juxtaposition, so the things
worth pinning are: that only companies with genuinely two views cost a model
call, that a failed call still leaves a readable row, that neither tracker's
own rows are ever touched, and that a merged row whose sources are gone does
not go on being served forever.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.services import merged_views
from app.services.merged_views import (
    FUSABLE_SOURCES,
    SOURCE_GOOAYE,
    SOURCE_MERGED,
    SOURCE_X,
    _combined_evidence,
    _display_name,
    _fingerprint,
    _is_stale,
    _mechanical_row,
    rebuild_merged_views,
)
from app.services.llm import MERGED_INSTRUCTIONS, _normalize_merged

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=30)


def _row(source, symbol="MU", stance="bullish", thesis="論點", name="Micron",
         strengths='["優勢一"]', weaknesses='["風險一"]', urls='["https://example.test/a"]',
         mentions=1, updated=OLD):
    return {
        "source": source, "symbol": symbol, "company_name": name, "stance": stance,
        "confidence": 0.6, "thesis": thesis, "strengths_json": strengths,
        "weaknesses_json": weaknesses, "evidence_urls_json": urls, "mention_count": mentions,
        "updated_at": updated,
    }


class FakeConnection:
    """Routes by statement shape, so the two SELECTs can differ."""

    def __init__(self, rows, merged_rows, fail_on=None):
        self.rows, self.merged_rows, self.fail_on = rows, merged_rows, fail_on
        self.executed = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.executed.append((sql, params))
        if self.fail_on and self.fail_on in sql.split()[0]:
            raise OperationalError("stmt", {}, Exception("database is down"))
        if sql.startswith("SELECT"):
            source = self.merged_rows if ":merged" in sql else self.rows
            return [type("R", (), {"_mapping": row})() for row in source]
        return type("R", (), {"rowcount": len(params) if isinstance(params, list)
                              else len(params.get("symbols", []))})()

    def __enter__(self): return self
    def __exit__(self, *exc): return False


class FakeEngine:
    def __init__(self, rows, merged_rows, fail_on=None):
        self.connection = FakeConnection(rows, merged_rows, fail_on)

    def connect(self): return self.connection
    def begin(self): return self.connection


@pytest.fixture
def wired(monkeypatch):
    def _wire(rows, synthesis=None, config=None, merged_rows=(), fail_on=None,
              state=None, engine_error=None):
        engine = FakeEngine(rows, list(merged_rows), fail_on)

        def get_engine():
            if engine_error:
                raise engine_error
            return engine

        monkeypatch.setattr(merged_views, "xstocktracker_engine", get_engine)
        engine.saved = []
        monkeypatch.setattr(merged_views, "_load_state", lambda: dict(state or {}))
        monkeypatch.setattr(merged_views, "_save_state",
                            lambda written, fused, doomed, now:
                            engine.saved.append((sorted(written), sorted(fused), list(doomed))))
        monkeypatch.setattr(merged_views, "load_runtime_config",
                            lambda: {"xstocktracker": config or {}})
        calls = []

        async def fake_synth(blocks, max_tokens=8192):
            calls.append(sorted(blocks))
            if synthesis == "fail":
                raise RuntimeError("provider down")
            thesis = "" if synthesis == "empty" else "融合論點"
            return {symbol: {"symbol": symbol, "stance": "mixed", "thesis": thesis,
                             "strengths": ["合併優勢"], "weaknesses": ["合併風險"]}
                    for symbol in blocks}

        monkeypatch.setattr(merged_views, "synthesize_merged_views", fake_synth)
        return engine, calls
    return _wire


def _written(engine):
    return next((p for sql, p in engine.connection.executed if sql.startswith("INSERT")), [])


def _deleted(engine):
    return [p["symbols"] for sql, p in engine.connection.executed if sql.startswith("DELETE")]


# --- the prompt contract -----------------------------------------------------

def test_prompt_forbids_listing_the_sources_separately():
    """The whole request: one opinion, not two labelled blocks."""
    assert "融合成一份觀點" in MERGED_INSTRUCTIONS
    assert "嚴禁" in MERGED_INSTRUCTIONS and "並列" in MERGED_INSTRUCTIONS


def test_prompt_does_not_ask_the_model_to_reweight_sources():
    """Both inputs are already weighted aggregates — X Stock Tracker applies
    its Serenity playbook bonus upstream. Re-weighting here would double-count."""
    assert "不需要再自行加權任何來源" in MERGED_INSTRUCTIONS


def test_prompt_warns_against_claiming_two_views_confirm_each_other():
    """Same company is not the same moment, and a recent rebuild of old
    material is not recent evidence — the stamp is an upper bound, so the
    prompt has to say both that and that closeness alone proves nothing."""
    assert "彙整更新於" in MERGED_INSTRUCTIONS
    assert "不是原始發言時間" in MERGED_INSTRUCTIONS
    assert "不得只因為時間接近" in MERGED_INSTRUCTIONS


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


def test_evidence_block_dates_each_view_without_overstating_it():
    """updated_at is when the tracker last rebuilt its aggregate, not when the
    episode or post happened; labelling it 資料時間 invited the model to read
    it as evidence recency."""
    evidence, _ = _combined_evidence({SOURCE_X: _row(SOURCE_X, updated=OLD)})
    assert "2026-08-12" in evidence and "非原始發言時間" in evidence


def test_display_name_prefers_a_real_name_over_a_repeated_ticker():
    sources = {SOURCE_X: _row(SOURCE_X, name="MU"), SOURCE_GOOAYE: _row(SOURCE_GOOAYE, name="Micron")}
    assert _display_name(sources, "MU") == "Micron"


def test_display_name_falls_back_to_the_symbol():
    assert _display_name({SOURCE_X: _row(SOURCE_X, name="")}, "MU") == "MU"


# --- the mechanical row ------------------------------------------------------

def test_agreeing_sources_keep_the_shared_stance():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, stance="bullish"),
                                 SOURCE_X: _row(SOURCE_X, stance="bullish")}, NOW)
    assert row["stance"] == "bullish" and row["source"] == SOURCE_MERGED


def test_disagreeing_sources_are_marked_mixed_not_silently_picked():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, stance="bullish"),
                                 SOURCE_X: _row(SOURCE_X, stance="bearish")}, NOW)
    assert row["stance"] == "mixed"


def test_mention_counts_are_summed_across_sources():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, mentions=3),
                                 SOURCE_X: _row(SOURCE_X, mentions=4)}, NOW)
    assert row["mention_count"] == 7


def test_points_from_both_sources_are_merged_and_deduplicated():
    row = _mechanical_row("MU", {
        SOURCE_GOOAYE: _row(SOURCE_GOOAYE, strengths='["共同優勢", "股癌獨有"]'),
        SOURCE_X: _row(SOURCE_X, strengths='["共同優勢", "X 獨有"]'),
    }, NOW)
    assert json.loads(row["strengths_json"]) == ["共同優勢", "股癌獨有", "X 獨有"]


def test_a_blank_gooaye_thesis_does_not_blank_out_the_merged_row():
    """Taking a fixed lead source published an empty thesis whenever Gooaye's
    row had none — and the unified view serves this row ahead of the one that
    did have text, so the company went blank in the bot."""
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, thesis="  "),
                                 SOURCE_X: _row(SOURCE_X, thesis="可用的論點")}, NOW)
    assert row["thesis"] == "可用的論點"


def test_both_theses_are_kept_when_the_model_did_not_run():
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(SOURCE_GOOAYE, thesis="股癌論點"),
                                 SOURCE_X: _row(SOURCE_X, thesis="X 論點")}, NOW)
    assert "股癌論點" in row["thesis"] and "X 論點" in row["thesis"]


def test_the_push_provenance_header_is_stripped_from_the_merged_text():
    """xstock_push stamps ［股癌 Gooaye · …］ so the row is identifiable in the
    other project's dashboard. The merged row is a view of the company, and
    the mechanical path has no model to drop the header for it."""
    row = _mechanical_row("MU", {SOURCE_GOOAYE: _row(
        SOURCE_GOOAYE, thesis="［股癌 Gooaye · US · 提及 3 集］\n真正的論點")}, NOW)
    assert row["thesis"] == "真正的論點"


def test_an_all_blank_company_still_produces_a_row():
    row = _mechanical_row("MU", {SOURCE_X: _row(SOURCE_X, thesis="")}, NOW)
    assert row["thesis"] == "" and row["symbol"] == "MU"


# --- what actually costs a model call ---------------------------------------

def test_only_companies_with_two_views_are_sent_to_the_model(wired):
    _, calls = wired([_row(SOURCE_GOOAYE, symbol="MU"), _row(SOURCE_X, symbol="MU"),
                      _row(SOURCE_GOOAYE, symbol="SOLO")])
    result = asyncio.run(rebuild_merged_views())
    assert calls == [["MU"]], "a single-source company has nothing to fuse"
    assert result["merged"] == 2 and result["two_sided"] == 1


def test_only_the_two_fusable_sources_are_read(wired):
    """`len(sources) > 1` counted any second source, so an unrecognised one
    bought a model call that then sent a single view's evidence."""
    engine, _ = wired([_row(SOURCE_GOOAYE)])
    asyncio.run(rebuild_merged_views())
    _, params = next((s, p) for s, p in engine.connection.executed
                     if s.startswith("SELECT") and ":sources" in s)
    assert params["sources"] == list(FUSABLE_SOURCES) == [SOURCE_GOOAYE, SOURCE_X]


def test_single_source_companies_still_get_a_merged_row(wired):
    engine, _ = wired([_row(SOURCE_GOOAYE, symbol="SOLO")])
    asyncio.run(rebuild_merged_views())
    written = _written(engine)
    assert [row["symbol"] for row in written] == ["SOLO"]
    assert written[0]["source"] == SOURCE_MERGED


def test_synthesis_result_replaces_the_mechanical_text(wired):
    engine, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)])
    asyncio.run(rebuild_merged_views())
    written = _written(engine)[0]
    assert written["thesis"] == "融合論點" and written["stance"] == "mixed"
    assert json.loads(written["strengths_json"]) == ["合併優勢"]


def test_a_failed_model_call_still_leaves_a_readable_row(wired):
    """Degrading to the mechanical rollup beats showing a blank company."""
    engine, _ = wired([_row(SOURCE_GOOAYE, thesis="股癌論點"), _row(SOURCE_X, thesis="")],
                      synthesis="fail")
    result = asyncio.run(rebuild_merged_views())
    assert _written(engine)[0]["thesis"] == "股癌論點"
    assert result["synthesized"] == 0
    assert result["failures"][0]["error"] == "RuntimeError"


def test_an_empty_thesis_from_the_model_is_reported_as_a_failure(wired):
    """The company was skipped but counted as answered, so the run reported
    synthesized=0 with no failures and nothing said which company was short."""
    _, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)], synthesis="empty")
    result = asyncio.run(rebuild_merged_views())
    assert result["synthesized"] == 0
    assert result["failures"] == [{"symbols": ["MU"], "error": "MissingCompanyResults"}]
    assert result["not_fused"] == 1


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
    select_sql, params = next((s, p) for s, p in engine.connection.executed
                              if s.startswith("SELECT") and ":sources" in s)
    assert SOURCE_MERGED not in params["sources"]


def test_only_merged_rows_are_written(wired):
    """Neither tracker's own rows may be modified by this pass."""
    engine, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)])
    asyncio.run(rebuild_merged_views())
    for sql, params in engine.connection.executed:
        if sql.startswith(("INSERT", "DELETE")):
            values = params if isinstance(params, list) else [params]
            assert all(v.get("source", v.get("merged")) == SOURCE_MERGED for v in values), sql


# --- removing merged rows whose sources are gone ----------------------------

def test_a_full_rebuild_removes_a_merged_row_whose_sources_all_vanished(wired):
    engine, _ = wired([_row(SOURCE_X, symbol="NVDA")],
                      merged_rows=[{"symbol": "MU", "updated_at": NOW}])
    result = asyncio.run(rebuild_merged_views())
    assert _deleted(engine) == [["MU"]] and result["removed"] == 1


def test_cleanup_runs_even_when_every_source_is_gone(wired):
    """Returning early on an empty source table left the stale merged rows
    being served by the unified view with nothing left to correct them."""
    engine, _ = wired([], merged_rows=[{"symbol": "MU", "updated_at": NOW}])
    result = asyncio.run(rebuild_merged_views())
    assert _deleted(engine) == [["MU"]] and result["removed"] == 1


def test_a_partial_rebuild_removes_the_requested_symbol_it_can_no_longer_build(wired):
    engine, _ = wired([_row(SOURCE_X, symbol="NVDA")],
                      merged_rows=[{"symbol": "MU", "updated_at": NOW}])
    asyncio.run(rebuild_merged_views(selected_symbols=["MU"]))
    assert _deleted(engine) == [["MU"]]


def test_a_partial_rebuild_never_removes_a_symbol_it_was_not_asked_about(wired):
    """The scoping that makes per-sync cleanup safe: a run that touched MU must
    not delete NVDA's merged row just because NVDA was not in this batch."""
    engine, _ = wired([_row(SOURCE_GOOAYE, symbol="MU")],
                      merged_rows=[{"symbol": "MU", "updated_at": OLD},
                                   {"symbol": "NVDA", "updated_at": OLD}])
    asyncio.run(rebuild_merged_views(selected_symbols=["MU"]))
    assert _deleted(engine) == []


def test_an_empty_symbol_list_does_nothing_at_all(wired):
    engine, _ = wired([_row(SOURCE_GOOAYE, symbol="MU")],
                      merged_rows=[{"symbol": "MU", "updated_at": OLD}])
    result = asyncio.run(rebuild_merged_views(selected_symbols=[]))
    assert result["skipped"] == "no symbols requested"
    assert engine.connection.executed == []


# --- not redoing work that is already current -------------------------------

def _record(sources, fused=True):
    """What a previous pass would have recorded for these sources."""
    return SimpleNamespace(fingerprint=_fingerprint(sources), fused=fused)


def _sources(*rows):
    return {row["source"]: row for row in rows}


def test_a_recorded_unchanged_company_is_left_alone(wired):
    gooaye, x = _row(SOURCE_GOOAYE), _row(SOURCE_X)
    engine, calls = wired([gooaye, x], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                          state={"MU": _record(_sources(gooaye, x))})
    result = asyncio.run(rebuild_merged_views())
    assert calls == [] and result["unchanged"] == 1 and result["merged"] == 0
    assert _written(engine) == []


def test_a_newer_source_row_triggers_a_re_fusion(wired):
    """What the scheduled refresh is for: the other tracker updates its own row
    and cannot call anything here, so nothing else would notice."""
    gooaye, x = _row(SOURCE_GOOAYE, updated=OLD), _row(SOURCE_X, updated=OLD)
    _, calls = wired([gooaye, _row(SOURCE_X, updated=NOW)],
                     merged_rows=[{"symbol": "MU", "updated_at": OLD}],
                     state={"MU": _record(_sources(gooaye, x))})
    assert asyncio.run(rebuild_merged_views())["merged"] == 1
    assert calls == [["MU"]]


def test_removing_one_source_is_noticed_even_though_nothing_got_newer(wired):
    """The timestamp test could not see this: the source left behind is older
    than the merged row, so the company went on serving a two-sided thesis and
    a summed mention count for a view that no longer existed."""
    gooaye, x = _row(SOURCE_GOOAYE), _row(SOURCE_X)
    engine, _ = wired([gooaye], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                      state={"MU": _record(_sources(gooaye, x))})
    result = asyncio.run(rebuild_merged_views())
    assert result["merged"] == 1 and result["unchanged"] == 0
    assert _written(engine)[0]["mention_count"] == 1  # no longer the sum of two


def test_a_failed_fusion_is_retried_on_the_next_pass(wired):
    """The fallback row is stamped 'now', so judging freshness by timestamp
    made every failure permanent: the next pass saw something newer than both
    sources and skipped the retry forever."""
    gooaye, x = _row(SOURCE_GOOAYE), _row(SOURCE_X)
    _, calls = wired([gooaye, x], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                     state={"MU": _record(_sources(gooaye, x), fused=False)})
    assert asyncio.run(rebuild_merged_views())["merged"] == 1
    assert calls == [["MU"]]


def test_a_no_llm_row_is_upgraded_once_the_model_is_enabled(wired):
    """The same trap from the other side: --no-llm wrote a current-looking row
    that a later run with the model enabled could never improve.

    While the model stays off there is nothing to upgrade *to*, so the company
    is left alone rather than having its mechanical row rewritten every tick;
    the debt is recorded and settled by the first run that can fuse.
    """
    gooaye, x = _row(SOURCE_GOOAYE), _row(SOURCE_X)
    wire = dict(merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                state={"MU": _record(_sources(gooaye, x), fused=False)})

    _, off = wired([gooaye, x], config={"merge_llm_enabled": False}, **wire)
    assert asyncio.run(rebuild_merged_views())["merged"] == 0 and off == []

    _, on = wired([gooaye, x], **wire)
    assert asyncio.run(rebuild_merged_views())["merged"] == 1 and on == [["MU"]]


def test_an_unfused_single_source_company_is_not_retried_forever(wired):
    """Nothing to fuse means the mechanical row is already final; retrying it
    every pass would undo the point of recording state at all."""
    gooaye = _row(SOURCE_GOOAYE)
    wired([gooaye], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
          state={"MU": _record(_sources(gooaye), fused=False)})
    assert asyncio.run(rebuild_merged_views())["unchanged"] == 1


def test_state_records_which_companies_actually_hold_a_fusion(wired):
    engine, _ = wired([_row(SOURCE_GOOAYE, symbol="MU"), _row(SOURCE_X, symbol="MU"),
                       _row(SOURCE_GOOAYE, symbol="SOLO")], synthesis="empty")
    asyncio.run(rebuild_merged_views())
    written, fused, _doomed = engine.saved[0]
    assert written == ["MU", "SOLO"]
    assert fused == ["SOLO"], "MU's model answer was empty, so it owes a retry"


def test_state_is_only_recorded_after_the_shared_table_commits(wired):
    """Recording a fusion that was never published would skip it next pass."""
    engine, _ = wired([_row(SOURCE_GOOAYE), _row(SOURCE_X)], fail_on="INSERT")
    asyncio.run(rebuild_merged_views())
    assert engine.saved == []


def test_state_for_a_removed_company_is_dropped_too(wired):
    engine, _ = wired([], merged_rows=[{"symbol": "MU", "updated_at": NOW}])
    asyncio.run(rebuild_merged_views())
    assert engine.saved[0][2] == ["MU"]


def test_a_full_pass_prunes_state_for_a_company_with_no_merged_row_either(wired):
    """Otherwise a record with nothing left to match it stays forever — which
    is how a symbol from a stray test run ended up lodged in a real database."""
    gooaye = _row(SOURCE_GOOAYE, symbol="MU")
    engine, _ = wired([gooaye], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                      state={"MU": _record(_sources(gooaye)), "GHOST": _record({})})
    asyncio.run(rebuild_merged_views())
    assert engine.saved[0][2] == ["GHOST"]


def test_a_partial_pass_does_not_prune_state_it_did_not_examine(wired):
    gooaye = _row(SOURCE_GOOAYE, symbol="MU")
    engine, _ = wired([gooaye], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                      state={"MU": _record(_sources(gooaye), fused=False), "OTHER": _record({})})
    asyncio.run(rebuild_merged_views(selected_symbols=["MU"]))
    assert engine.saved[0][2] == []


def test_force_rebuilds_even_an_up_to_date_company(wired):
    gooaye, x = _row(SOURCE_GOOAYE), _row(SOURCE_X)
    _, calls = wired([gooaye, x], merged_rows=[{"symbol": "MU", "updated_at": NOW}],
                     state={"MU": _record(_sources(gooaye, x))})
    assert asyncio.run(rebuild_merged_views(force=True))["merged"] == 1
    assert calls == [["MU"]]


@pytest.mark.parametrize("merged_at,record,expected", [
    (None, "match", True),        # no merged row in the shared table at all
    (NOW, "none", True),          # this project never recorded writing one
    (NOW, "other", True),         # built from a different set of sources
    (NOW, "match", False),        # recorded, current, and fused
])
def test_staleness_rules(merged_at, record, expected):
    sources = _sources(_row(SOURCE_X, updated=OLD))
    records = {"match": _record(sources), "none": None,
               "other": _record(_sources(_row(SOURCE_X, updated=NOW)))}
    assert _is_stale(sources, merged_at, records[record], want_fusion=True) is expected


def test_a_source_row_with_no_timestamp_counts_as_stale():
    """Redoing work needlessly is recoverable; serving a stale opinion is not."""
    sources = _sources(_row(SOURCE_X, updated=None))
    assert _is_stale(sources, NOW, _record(sources), want_fusion=True) is True


def test_naive_timestamps_do_not_crash_the_fingerprint():
    sources = _sources(_row(SOURCE_X, updated=datetime(2026, 9, 11)))
    assert _is_stale(sources, NOW, _record(sources), want_fusion=True) is False


# --- the other project's database is not this project's problem --------------

def test_a_database_read_failure_is_reported_not_raised(wired):
    """By the time this runs the local sync has already committed. A shared
    database that is down, absent or unmigrated must not fail that run."""
    wired([_row(SOURCE_GOOAYE)], fail_on="SELECT")
    result = asyncio.run(rebuild_merged_views())
    assert result["error"] == "OperationalError" and "讀取" in result["skipped"]


def test_a_database_write_failure_is_reported_not_raised(wired):
    wired([_row(SOURCE_GOOAYE)], fail_on="INSERT")
    result = asyncio.run(rebuild_merged_views())
    assert result["error"] == "OperationalError" and "寫入" in result["skipped"]


def test_a_bad_dsn_is_reported_not_raised(wired):
    """Building the engine parses the DSN and can raise on its own, so it has
    to happen inside the guard rather than one line above it."""
    wired([], engine_error=ValueError("Could not parse SQLAlchemy URL"))
    result = asyncio.run(rebuild_merged_views())
    assert result["error"] == "ValueError"
