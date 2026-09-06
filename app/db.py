from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import Base


def _psycopg_dsn(dsn: str) -> str:
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return dsn


engine = create_engine(_psycopg_dsn(get_settings().postgres_dsn), pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)


def postgres_health() -> bool:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
