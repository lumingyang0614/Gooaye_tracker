"""Live round trip against the configured X Stock Tracker database.

Skipped entirely when XSTOCKTRACKER_DB_URL isn't set or the database is
unreachable, so the suite still runs on a machine that only has Gooaye.

What the hermetic tests in test_xstock_push.py cannot check is whether the
*other* project's schema actually accepts what this one writes: the grants,
the `source` column, and above all the UNIQUE (source, symbol) key that lets
one symbol exist once per tracker. Everything here runs inside a transaction
that is always rolled back, so a real database is never modified.
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.xstock_push import SOURCE_GOOAYE, TABLE, _UPSERT
from app.services.securities import xstocktracker_engine


@pytest.fixture
def connection():
    """A transaction on the X Stock Tracker database, always rolled back."""
    engine = xstocktracker_engine()
    if engine is None:
        pytest.skip("XSTOCKTRACKER_DB_URL 未設定")
    try:
        conn = engine.connect()
    except SQLAlchemyError as exc:
        pytest.skip(f"X Stock Tracker 資料庫無法連線：{exc}")
    transaction = conn.begin()
    try:
        if not conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = 'source'"
        ), {"t": TABLE}).first():
            pytest.skip(f"{TABLE} 尚未執行 migrate_company_view_source")
        yield conn
    finally:
        transaction.rollback()
        conn.close()


def _row(symbol, **overrides):
    values = {
        "source": SOURCE_GOOAYE, "symbol": symbol, "company_name": "測試公司",
        "mention_count": 1, "stance": "bullish", "confidence": 0.5,
        "thesis": "［股癌 Gooaye · TW · 提及 1 集］\n測試", "strengths_json": "[]",
        "weaknesses_json": "[]", "evidence_urls_json": "[]",
        "updated_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return values


def test_role_can_write_the_shared_company_view_table(connection):
    connection.execute(_UPSERT, [_row("ZZTEST.TW")])
    stored = connection.execute(text(
        f"SELECT source, stance FROM {TABLE} WHERE source = :s AND symbol = 'ZZTEST.TW'"
    ), {"s": SOURCE_GOOAYE}).one()
    assert stored[0] == SOURCE_GOOAYE and stored[1] == "bullish"


def test_same_symbol_can_exist_once_per_source(connection):
    """The reason the unique key had to become (source, symbol).

    Borrows a symbol X Stock Tracker already holds; under the old
    UNIQUE(symbol) index this insert failed and Gooaye could never publish a
    view on any company X had already covered.
    """
    existing = connection.execute(text(
        f"SELECT symbol FROM {TABLE} WHERE source = 'x' LIMIT 1")).scalar()
    if existing is None:
        pytest.skip("x_company_views 目前沒有 source='x' 的資料可供對照")

    connection.execute(_UPSERT, [_row(existing, stance="bearish")])
    rows = dict(connection.execute(text(
        f"SELECT source, stance FROM {TABLE} WHERE symbol = :symbol"
    ), {"symbol": existing}).all())
    # A superset check, not equality: a third source ('merged', written by
    # app/services/merged_views.py) may also hold this symbol, and that is
    # exactly the coexistence this test is about.
    assert {"x", SOURCE_GOOAYE} <= set(rows)
    assert rows[SOURCE_GOOAYE] == "bearish"


def test_upsert_updates_in_place_rather_than_duplicating(connection):
    connection.execute(_UPSERT, [_row("ZZTEST.TW", stance="bullish")])
    connection.execute(_UPSERT, [_row("ZZTEST.TW", stance="bearish", mention_count=7)])
    rows = connection.execute(text(
        f"SELECT stance, mention_count FROM {TABLE} WHERE source = :s AND symbol = 'ZZTEST.TW'"
    ), {"s": SOURCE_GOOAYE}).all()
    assert rows == [("bearish", 7)]


def test_gooaye_role_still_cannot_read_the_other_project_articles(connection):
    """The integration granted write access to one table and nothing else.

    x_articles holds X Stock Tracker's own source material; Gooaye has no
    business reading it, and a future GRANT that over-reaches should fail here.
    """
    allowed = connection.execute(text(
        "SELECT has_table_privilege(current_user, 'x_articles', 'SELECT')")).scalar()
    if connection.execute(text("SELECT current_user")).scalar() == "postgres":
        pytest.skip("以 superuser 連線，權限檢查無意義")
    assert allowed is False
