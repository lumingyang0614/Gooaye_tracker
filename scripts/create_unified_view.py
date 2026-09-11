"""Create the merged company-view used by downstream consumers (the LINE bot).

`x_company_views` holds one row per company *per source*, so a consumer that
looks a company up by name gets two results for anything both trackers cover
— which is why `/觀點 MU` showed two cards. This creates a read-only view,
`company_views_unified`, with one row per company that folds both sources
together. No table is altered and nothing is written, so both trackers keep
working exactly as they do now.

    python -m scripts.create_unified_view
    python -m scripts.create_unified_view --dsn "postgresql://postgres:pw@localhost:5432/xstocktracker"
    python -m scripts.create_unified_view --print-sql

Creating a view needs CREATE on the schema, which the read/write account this
project normally uses does not have — it was granted only what it needs to
write its own rows. So either pass a privileged DSN with --dsn, or use
--print-sql and run the output as an administrator. Re-running is safe.

Afterwards, point the consumer at the view. For the LINE bot that is one line
in its .env, no code change:

    XSTOCKTRACKER_COMPANY_VIEWS_TABLE=public.company_views_unified
"""
from __future__ import annotations

import argparse

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.services.xstock_unified_view import VIEW_NAME, full_sql, statements


def _psycopg(dsn: str) -> str:
    return dsn.replace("postgresql://", "postgresql+psycopg://", 1) if dsn.startswith("postgresql://") else dsn


def _own_role(default_dsn: str) -> str:
    """The account this project uses — it needs SELECT to verify the view."""
    try:
        return make_url(_psycopg(default_dsn)).username or ""
    except Exception:
        return ""


def _existing_readers(connection) -> list[str]:
    """Roles that can already SELECT the view, so a rebuild does not cut them off.

    Read from the catalog rather than information_schema.role_table_grants:
    that view only reports grants whose grantor or grantee is a currently
    enabled role, so a grant to PUBLIC — the one that silently gives every
    consumer access — does not appear there at all, and dropping the view
    would have revoked it with nothing to put it back.

    aclexplode reports PUBLIC as grantee 0. A relacl of NULL means no explicit
    grants have ever been made, and to_regclass yields NULL (matching no rows)
    when the view does not exist yet, so the first run finds nothing.
    """
    rows = connection.execute(text("""
        SELECT DISTINCT
            CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END AS grantee
        FROM pg_class c, aclexplode(c.relacl) a
        WHERE c.oid = to_regclass(:view) AND a.privilege_type = 'SELECT'
    """), {"view": VIEW_NAME}).scalars().all()
    return [str(role) for role in rows if role]


def main() -> None:
    parser = argparse.ArgumentParser(description="建立跨來源合併的公司觀點 view")
    parser.add_argument("--dsn", default="", help="有 CREATE 權限的連線字串（預設用 XSTOCKTRACKER_DB_URL）")
    parser.add_argument("--print-sql", action="store_true", help="只印出 SQL，不連線")
    parser.add_argument("--reader-role", action="append", default=[],
                        help="額外授予 SELECT 的角色，可重複指定")
    args = parser.parse_args()

    configured = get_settings().xstocktracker_db_url.strip()
    readers = [role for role in [_own_role(configured), *args.reader_role] if role]

    if args.print_sql:
        print(full_sql(readers))
        return

    dsn = args.dsn.strip() or configured
    if not dsn:
        raise SystemExit(
            "沒有可用的連線字串。請在 .env 設定 XSTOCKTRACKER_DB_URL，或用 --dsn 指定。"
        )

    engine = create_engine(_psycopg(dsn), pool_pre_ping=True)
    try:
        with engine.begin() as connection:
            # Re-running drops the view, and dropping it drops its grants. A
            # consumer that connects as its own account would silently lose
            # access unless whoever re-ran this happened to remember to pass
            # --reader-role for it, so carry the existing grantees forward.
            readers = _existing_readers(connection) + [r for r in readers if r]
            for statement in statements(readers):
                connection.execute(text(statement))
            total = connection.execute(text(f"SELECT count(*) FROM {VIEW_NAME}")).scalar()
            merged = connection.execute(text(
                f"SELECT count(*) FROM {VIEW_NAME} WHERE source = 'merged'")).scalar()
    except SQLAlchemyError as exc:
        if "permission denied" in str(exc).lower():
            raise SystemExit(
                f"目前的帳號沒有建立 view 的權限。\n\n"
                "兩個做法擇一：\n"
                "  1. 用有權限的帳號重跑（X Stock Tracker 的 .env 裡 POSTGRES_DSN 通常就是）：\n"
                '     python -m scripts.create_unified_view --dsn "postgresql://postgres:密碼@localhost:5432/xstocktracker"\n'
                "  2. 印出 SQL 自行以管理員身分執行：\n"
                "     python -m scripts.create_unified_view --print-sql\n"
            ) from exc
        raise SystemExit(f"建立失敗：{type(exc).__name__}: {exc}") from exc

    print(f"已建立 {VIEW_NAME}")
    print(f"  公司數          : {total}（一家一列）")
    print(f"  已有融合觀點的  : {merged}")
    if merged < total:
        print(f"  尚未融合的 {total - merged} 家暫時沿用單一來源，跑 "
              "python -m scripts.rebuild_merged_views 產生")
    if readers:
        print(f"  已授予 SELECT : {', '.join(readers)}")
    print()
    print("接著把讀取端指向這個 view。以 LINE bot 為例，改 .env 一行即可（不用改程式）：")
    print(f"  XSTOCKTRACKER_COMPANY_VIEWS_TABLE={VIEW_NAME}")


if __name__ == "__main__":
    main()
