"""Pushing Gooaye company insights into X Stock Tracker's x_company_views.

The push writes to a *second, optional* database owned by another project, so
the properties that matter most here are the defensive ones: it must never
raise into a sync run, never touch rows belonging to the other tracker, and
never leave a row behind for a company Gooaye has stopped tracking.

The statements are exercised against a recording fake rather than a live
Postgres, so the suite stays hermetic; test_xstock_push_live.py covers the
real round trip when a database happens to be configured.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

from app.services import xstock_push
from app.services.xstock_push import SOURCE_GOOAYE, push_company_insights


def _insight(symbol="2330.TW", **overrides):
    values = {
        "symbol": symbol,
        "company_name": "台積電",
        "market": "TW",
        "mention_count": 3,
        "stance": "bullish",
        "confidence": 0.72,
        "thesis": "先進製程供不應求。",
        "strengths_json": '["製程領先"]',
        "weaknesses_json": '["地緣風險"]',
        "evidence_urls_json": '["https://example.test/ep693"]',
        "updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeConnection:
    """Records every statement, and answers the two preflight probes separately.

    They are separate because they produce different operator instructions:
    a missing table means the other project was never initialized, a missing
    `source` column means it was initialized but not migrated.
    """

    def __init__(self, table_exists=True, column_exists=True, delete_rowcount=0, fail_on=None):
        self.table_exists = table_exists
        self.column_exists = column_exists
        self.delete_rowcount = delete_rowcount
        self.fail_on = fail_on
        self.calls = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        self.calls.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise OperationalError("boom", None, Exception("connection lost"))
        if "information_schema.tables" in sql:
            return SimpleNamespace(first=lambda: (1,) if self.table_exists else None)
        if "information_schema.columns" in sql:
            return SimpleNamespace(first=lambda: (1,) if self.column_exists else None)
        if sql.startswith("DELETE"):
            return SimpleNamespace(rowcount=self.delete_rowcount)
        return SimpleNamespace(rowcount=len(params) if isinstance(params, list) else 1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection


@pytest.fixture
def wired(monkeypatch):
    """Point the push at a recording connection with one insight to send."""
    def _wire(insights=None, **kwargs):
        connection = FakeConnection(**kwargs)
        monkeypatch.setattr(xstock_push, "xstocktracker_engine", lambda: FakeEngine(connection))
        monkeypatch.setattr(xstock_push, "load_runtime_config",
                            lambda: {"xstocktracker": {"push_company_insights": True}})
        rows = [_insight()] if insights is None else insights
        # Stub out only the half that reads Gooaye's own database; the mapping
        # from insight to row is the part under test, so _build reuses it.
        monkeypatch.setattr(xstock_push, "_rows_to_push", lambda selected: _build(rows, selected))
        return connection
    return _wire


def _build(insights, selected):
    """Stand in for the DB read half of _rows_to_push, keeping the mapping half."""
    chosen = [i for i in insights if selected is None or i.symbol.upper() in selected]
    return [{
        "source": SOURCE_GOOAYE,
        "symbol": i.symbol.strip().upper(),
        "company_name": i.company_name,
        "mention_count": i.mention_count,
        "stance": i.stance,
        "confidence": i.confidence,
        "thesis": xstock_push._thesis_with_provenance(i),
        "strengths_json": xstock_push._json_list(i.strengths_json),
        "weaknesses_json": xstock_push._json_list(i.weaknesses_json),
        "evidence_urls_json": xstock_push._json_list(i.evidence_urls_json),
        "updated_at": i.updated_at,
    } for i in chosen]


# --- provenance and normalization (pure functions, no DB) --------------------

def test_thesis_is_labelled_with_source_market_and_mentions():
    # X Stock Tracker renders these next to its own X-post rows; without the
    # label a 股癌 thesis reads as if a tweet had said it.
    thesis = xstock_push._thesis_with_provenance(_insight())
    assert thesis.startswith("［股癌 Gooaye · TW · 提及 3 集］")
    assert "先進製程供不應求。" in thesis


def test_thesis_label_survives_an_empty_thesis():
    assert xstock_push._thesis_with_provenance(_insight(thesis="")) == "［股癌 Gooaye · TW · 提及 3 集］"


def test_unknown_market_is_labelled_not_guessed():
    assert "· UNKNOWN ·" in xstock_push._thesis_with_provenance(_insight(market=""))


@pytest.mark.parametrize("stored,expected", [
    ('["a"]', '["a"]'), (None, "[]"), ("not json", "[]"), ('{"a": 1}', "[]"), ("[]", "[]"),
])
def test_corrupt_json_columns_degrade_to_empty_lists(stored, expected):
    # A single unparseable column must not cost the whole push.
    assert xstock_push._json_list(stored) == expected


def test_json_list_keeps_chinese_unescaped():
    assert xstock_push._json_list('["製程領先"]') == '["製程領先"]'


# --- skip paths --------------------------------------------------------------

def test_push_skipped_when_disabled_in_config(monkeypatch):
    monkeypatch.setattr(xstock_push, "load_runtime_config",
                        lambda: {"xstocktracker": {"push_company_insights": False}})
    result = push_company_insights()
    assert result["skipped"] == "xstocktracker.push_company_insights=false"
    assert result["pushed"] == 0


def test_push_skipped_when_no_database_configured(monkeypatch):
    monkeypatch.setattr(xstock_push, "load_runtime_config", lambda: {})
    monkeypatch.setattr(xstock_push, "xstocktracker_engine", lambda: None)
    assert push_company_insights()["skipped"] == "XSTOCKTRACKER_DB_URL 未設定"


def test_empty_symbol_list_is_a_skip_not_a_full_push(monkeypatch, wired):
    # An incremental call with nothing affected must not be mistaken for a
    # full push, which would delete every gooaye row it doesn't rewrite.
    connection = wired()
    result = push_company_insights(selected_symbols=[])
    assert result["skipped"] == "no affected symbols"
    assert connection.calls == []


# --- failure containment -----------------------------------------------------

def test_database_failure_is_reported_not_raised(wired):
    wired(fail_on="INSERT")
    result = push_company_insights()
    assert "OperationalError" in result["error"]
    assert result["pushed"] == 0


def test_unmigrated_target_table_gives_an_actionable_message(wired):
    # Table is there but has no `source` column: X Stock Tracker exists and
    # was never migrated, so name the migration, not the initializer.
    wired(column_exists=False)
    result = push_company_insights()
    assert "migrate_company_view_source" in result["error"]
    assert result["pushed"] == 0


def test_missing_target_table_points_at_the_other_project_initializer(wired):
    wired(table_exists=False)
    assert "init_postgres" in push_company_insights()["error"]


def test_preflight_failure_writes_nothing(wired):
    connection = wired(column_exists=False)
    push_company_insights()
    assert not [sql for sql, _ in connection.calls if sql.startswith(("INSERT", "DELETE"))]


# --- what actually gets written ---------------------------------------------

def test_full_push_writes_rows_under_the_gooaye_source(wired):
    connection = wired()
    result = push_company_insights()
    insert = next(params for sql, params in connection.calls if sql.startswith("INSERT"))
    assert [row["source"] for row in insert] == [SOURCE_GOOAYE]
    assert insert[0]["symbol"] == "2330.TW"
    assert result["pushed"] == 1 and result["mode"] == "all"


def test_every_statement_is_scoped_to_the_gooaye_source(wired):
    # The whole safety story rests on this: X's rows must be unreachable.
    connection = wired(insights=[_insight(), _insight("NVDA", company_name="NVIDIA")])
    push_company_insights()
    for sql, params in connection.calls:
        if sql.startswith(("INSERT", "DELETE")):
            values = params if isinstance(params, list) else [params]
            assert all(value.get("source") == SOURCE_GOOAYE for value in values), sql


def test_full_push_deletes_gooaye_rows_for_companies_no_longer_tracked(wired):
    connection = wired(delete_rowcount=2)
    result = push_company_insights()
    delete_sql, params = next((s, p) for s, p in connection.calls if s.startswith("DELETE"))
    assert "NOT (symbol = ANY(:live))" in delete_sql
    assert params["live"] == ["2330.TW"]
    assert result["removed"] == 2


def test_full_push_with_no_companies_clears_every_gooaye_row(wired):
    """A full push mirrors Gooaye exactly, including "Gooaye tracks nothing".

    Verified against real Postgres to accept the empty array rather than error.
    This only triggers when the local read genuinely returned no companies —
    a database failure raises out of _rows_to_push before this point, so a
    broken connection can never be mistaken for an empty result.
    """
    connection = wired(insights=[], delete_rowcount=9)
    result = push_company_insights()
    assert not [sql for sql, _ in connection.calls if sql.startswith("INSERT")]
    delete_sql, params = next((s, p) for s, p in connection.calls if s.startswith("DELETE"))
    assert params["live"] == [] and params["source"] == SOURCE_GOOAYE
    assert result["removed"] == 9


def test_incremental_push_only_deletes_requested_symbols_that_vanished(wired):
    # 3008.TW was affected this run but no longer has an insight (its only
    # mention was dropped on re-analysis), so its stale row must go — while
    # every gooaye company outside this run is left completely alone.
    connection = wired(delete_rowcount=1)
    push_company_insights(selected_symbols=["2330.TW", "3008.TW"])
    delete_sql, params = next((s, p) for s, p in connection.calls if s.startswith("DELETE"))
    assert "symbol = ANY(:stale)" in delete_sql
    assert params["stale"] == ["3008.TW"]


def test_incremental_push_issues_no_delete_when_nothing_vanished(wired):
    connection = wired()
    result = push_company_insights(selected_symbols=["2330.TW"])
    assert not [sql for sql, _ in connection.calls if sql.startswith("DELETE")]
    assert result["removed"] == 0 and result["mode"] == "incremental"


def test_requested_symbols_are_normalized_before_matching(wired):
    connection = wired()
    push_company_insights(selected_symbols=["  2330.tw  "])
    assert [row["symbol"] for row in next(p for s, p in connection.calls if s.startswith("INSERT"))] == ["2330.TW"]
