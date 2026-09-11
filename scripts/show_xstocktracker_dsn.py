"""Print which database account this project uses for X Stock Tracker.

The integration's one manual step is a GRANT issued from the *other* project,
and that GRANT needs the role name this project logs in as — which lives in
this project's .env and is invisible from over there. Running
`migrate_company_view_source` with the wrong name fails with "資料庫沒有 X
這個角色", so this prints the right answer to paste in.

    python -m scripts.show_xstocktracker_dsn

The password is never printed.
"""
from __future__ import annotations

from sqlalchemy.engine import make_url

from app.config import get_settings


def main() -> None:
    raw = get_settings().xstocktracker_db_url.strip()
    if not raw:
        print("XSTOCKTRACKER_DB_URL 未設定——整合會自動略過。")
        print("請先在 .env 填入 X Stock Tracker 資料庫的連線字串。")
        return

    try:
        url = make_url(raw)
    except Exception as exc:
        raise SystemExit(f"XSTOCKTRACKER_DB_URL 格式無法解析：{exc}")

    print(f"連線位址 : {url.host}:{url.port or 5432}/{url.database}")
    print(f"登入帳號 : {url.username}")
    print()
    print("到 X Stock Tracker 專案目錄，用它自己的 venv 執行：")
    print(f"  python -m scripts.migrate_company_view_source --writer-role {url.username}")


if __name__ == "__main__":
    main()
