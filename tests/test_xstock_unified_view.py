"""The view that hands consumers exactly one row per company.

x_company_views keeps one row per source on purpose, so a consumer that looks
a company up by name and shows every match showed the same company twice —
the LINE bot's `/觀點 MU` returned two cards. This view resolves that at read
time by preferring the LLM-fused `merged` row, falling back to a single
source for a company the merge has not reached yet.

The statement builder is checked directly; the live section runs the real SQL
against a configured database inside a rolled-back transaction, so nothing is
created permanently and the file skips entirely without a database.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.securities import xstocktracker_engine
from app.services.xstock_unified_view import (
    SOURCE_TABLE,
    VIEW_NAME,
    full_sql,
    statements,
)


# --- the statement builder (no database) ------------------------------------

def test_view_is_dropped_before_it_is_created():
    """CREATE OR REPLACE cannot remove a column from an existing view, so a
    changed definition fails with "cannot drop columns from view" unless the
    old one is dropped first. This bit on the first definition change."""
    sql = statements([])
    assert sql[0] == f"DROP VIEW IF EXISTS {VIEW_NAME}"
    assert any(f"CREATE OR REPLACE VIEW {VIEW_NAME}" in statement for statement in sql)


def test_grants_come_after_the_view_exists():
    sql = statements(["gooaye"])
    create = next(i for i, s in enumerate(sql) if "CREATE OR REPLACE VIEW" in s)
    grant = next(i for i, s in enumerate(sql) if s.startswith("GRANT"))
    assert grant > create


def test_no_reader_roles_means_no_grants():
    assert not any(statement.startswith("GRANT") for statement in statements([]))


def test_blank_role_names_are_ignored():
    assert not any(statement.startswith("GRANT") for statement in statements(["", "   ", None]))


def test_reader_roles_are_quoted():
    assert '"gooaye"' in next(s for s in statements(["gooaye"]) if s.startswith("GRANT"))


def test_merged_rows_win_over_single_source_rows():
    """The ordering is the whole behaviour: without it a consumer could get
    either tracker's raw row instead of the fused one."""
    sql = full_sql([])
    assert "WHEN 'merged' THEN 0" in sql
    assert "DISTINCT ON" in sql


def test_definition_never_writes_to_a_table():
    """The safety claim: adding this must not change either tracker's data.

    DROP VIEW/FUNCTION are this module's own objects; anything touching a
    *table* is what must never appear.
    """
    sql = full_sql(["gooaye"]).upper()
    for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE", "ALTER TABLE", "TRUNCATE"):
        assert forbidden not in sql, f"the view definition must not contain {forbidden}"


def test_view_reads_the_shared_company_table():
    assert SOURCE_TABLE in full_sql([])


def test_printable_sql_is_statement_terminated():
    assert full_sql(["gooaye"]).rstrip().endswith(";")


@pytest.mark.parametrize("column", [
    "symbol", "company_name", "thesis", "strengths_json", "weaknesses_json",
    "evidence_urls_json", "stance", "mention_count", "updated_at",
])
def test_view_exposes_the_column_names_consumers_probe_for(column):
    """The consumer finds fields by name, so the view keeps the table's
    spelling — that is what makes switching it a config change, not a code one."""
    assert column in full_sql([])


# --- the real SQL, against a real database ----------------------------------

@pytest.fixture
def connection():
    engine = xstocktracker_engine()
    if engine is None:
        pytest.skip("XSTOCKTRACKER_DB_URL 未設定")
    try:
        conn = engine.connect()
    except SQLAlchemyError as exc:
        pytest.skip(f"資料庫無法連線：{exc}")
    transaction = conn.begin()
    try:
        if not conn.execute(text(
            "SELECT 1 FROM information_schema.views WHERE table_name = 'company_views_unified'"
        )).first():
            pytest.skip("company_views_unified 尚未建立（跑 scripts.create_unified_view）")
        yield conn
    finally:
        transaction.rollback()
        conn.close()


def _insert(connection, source, symbol, stance, thesis, updated="now()"):
    connection.execute(text(f"""
        INSERT INTO {SOURCE_TABLE}
            (source, symbol, company_name, mention_count, stance, confidence,
             thesis, strengths_json, weaknesses_json, evidence_urls_json, updated_at)
        VALUES (:source, :symbol, 'Ztest', 1, :stance, 0.5, :thesis, '[]', '[]', '[]', {updated})
    """), {"source": source, "symbol": symbol, "stance": stance, "thesis": thesis})


def test_one_row_per_company(connection):
    duplicated = connection.execute(text(
        f"SELECT symbol, count(*) FROM {VIEW_NAME} GROUP BY symbol HAVING count(*) > 1")).all()
    assert duplicated == [], f"the view must collapse sources, found: {duplicated}"


def test_every_company_in_the_table_is_present(connection):
    """Collapsing must not silently drop a company."""
    table_symbols = connection.execute(text(
        f"SELECT count(DISTINCT upper(btrim(symbol))) FROM {SOURCE_TABLE}")).scalar()
    view_symbols = connection.execute(text(f"SELECT count(*) FROM {VIEW_NAME}")).scalar()
    assert view_symbols == table_symbols


def test_the_merged_row_is_the_one_returned(connection):
    """Even when the single-source rows are newer — freshness must not beat
    the fused view, or a push would knock the merged answer out."""
    _insert(connection, "x", "ZZPICK", "bearish", "x 的說法", "now() + interval '1 day'")
    _insert(connection, "gooaye", "ZZPICK", "bearish", "股癌的說法", "now() + interval '1 day'")
    _insert(connection, "merged", "ZZPICK", "bullish", "融合後的觀點", "now() - interval '1 day'")

    source, stance, thesis = connection.execute(text(
        f"SELECT source, stance, thesis FROM {VIEW_NAME} WHERE symbol = 'ZZPICK'")).one()
    assert source == "merged"
    assert stance == "bullish" and thesis == "融合後的觀點"


def test_a_company_without_a_merged_row_still_appears(connection):
    """The fallback: a company the merge has not reached yet must not vanish."""
    _insert(connection, "gooaye", "ZZSOLO", "bearish", "只有股癌談過")
    source, stance = connection.execute(text(
        f"SELECT source, stance FROM {VIEW_NAME} WHERE symbol = 'ZZSOLO'")).one()
    assert source == "gooaye" and stance == "bearish"


def test_gooaye_is_preferred_over_x_when_neither_is_merged(connection):
    # Only a tie-break for the un-merged fallback; 股癌 carries the readable
    # company name and the Taiwan names this project resolves.
    _insert(connection, "x", "ZZBOTH", "bearish", "x 的說法")
    _insert(connection, "gooaye", "ZZBOTH", "bullish", "股癌的說法")
    source = connection.execute(text(
        f"SELECT source FROM {VIEW_NAME} WHERE symbol = 'ZZBOTH'")).scalar()
    assert source == "gooaye"


def test_symbols_are_normalized_so_case_does_not_split_a_company(connection):
    _insert(connection, "gooaye", " zzcase ", "bullish", "小寫加空白")
    rows = connection.execute(text(
        f"SELECT symbol FROM {VIEW_NAME} WHERE symbol = 'ZZCASE'")).all()
    assert len(rows) == 1


# --- grants survive a rebuild ------------------------------------------------

def test_public_is_not_quoted_as_if_it_were_a_role_name():
    """GRANT ... TO "PUBLIC" creates a grant to a role nobody has; the keyword
    has to go through bare or the rebuild quietly revokes everyone's access."""
    granted = [s for s in statements(["PUBLIC"]) if s.startswith("GRANT")]
    assert granted == [f"GRANT SELECT ON {VIEW_NAME} TO PUBLIC"]


def test_roles_are_not_granted_twice():
    """Same role, written with stray whitespace. Case is *not* included here:
    PostgreSQL quoted identifiers are case-sensitive, so "Gooaye" is a
    different role — see test_role_names_differing_only_in_case_are_kept_apart."""
    granted = [s for s in statements(["gooaye", " gooaye ", "gooaye"]) if s.startswith("GRANT")]
    assert len(granted) == 1


# --- grant targets are identifiers, not string interpolation -----------------

def test_role_names_differing_only_in_case_are_kept_apart():
    """A quoted identifier is case-sensitive in PostgreSQL, so "Reader" and
    "reader" are two different roles; folding them together drops a grant."""
    granted = [s for s in statements(["Reader", "reader"]) if s.startswith("GRANT")]
    assert len(granted) == 2
    assert any('"Reader"' in s for s in granted) and any('"reader"' in s for s in granted)


def test_public_is_still_deduplicated_regardless_of_case():
    """PUBLIC is a keyword rather than a name, so case carries no meaning."""
    granted = [s for s in statements(["PUBLIC", "public", "Public"]) if s.startswith("GRANT")]
    assert granted == [f"GRANT SELECT ON {VIEW_NAME} TO PUBLIC"]


def test_a_double_quote_in_a_role_name_is_escaped():
    """Otherwise the name closes its own quoting and the remainder is SQL."""
    granted = next(s for s in statements(['we"ird']) if s.startswith("GRANT"))
    assert granted.endswith('"we""ird"')


def test_printable_sql_is_wrapped_in_a_transaction():
    """The script drops the view first. A paste that fails partway through —
    a typo'd role, a refused GRANT — must not leave consumers with no view."""
    sql = full_sql(["gooaye"])
    assert sql.startswith("BEGIN;")
    assert sql.rstrip().endswith("COMMIT;")
    assert sql.index("DROP VIEW") > sql.index("BEGIN;")
    assert sql.index("COMMIT;") > sql.index("GRANT")
