from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db, postgres_health
from app.services.company_insights import list_symbol_segments, rebuild_company_insights
from app.services.dashboard import build_dashboard, episodes_page
from app.services.llm import llm_health
from app.services.sync import sync_gooaye
from app.services.taiwan_securities import sync_taiwan_securities

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if postgres_health():
        init_db()
    yield


app = FastAPI(title=get_settings().app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/dashboard")
def dashboard():
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    return build_dashboard()


@app.get("/api/episodes")
def episodes(page: int = 1, page_size: int = 12):
    if page < 1 or not 1 <= page_size <= 50:
        raise HTTPException(400, "page must be >= 1 and page_size must be between 1 and 50")
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    return episodes_page(page=page, page_size=page_size)


@app.get("/api/companies/{symbol}/segments")
def company_segments(symbol: str):
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    return {"symbol": symbol.strip().upper(), "segments": list_symbol_segments(symbol)}


@app.post("/api/sync")
async def sync(limit: int | None = None):
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    return await sync_gooaye(limit=limit)


@app.post("/api/companies/rebuild")
async def rebuild_companies(symbols: str | None = None, use_llm: bool = True):
    """Re-derive company insights from the segments already in the database.

    Runs no transcript sync and no episode analysis, so it is the cheap way to
    pick up an edited config/settings.yaml alias list or a freshly synced
    Taiwan security master. `use_llm=false` skips the per-company narrative
    pass entirely (no model calls at all).
    """
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    selected = [value.strip() for value in symbols.split(",") if value.strip()] if symbols else None
    if symbols is not None and not selected:
        raise HTTPException(400, "symbols must contain at least one symbol")
    return await rebuild_company_insights(selected_symbols=selected, use_llm=use_llm)


@app.post("/api/securities/taiwan/sync")
async def sync_tw_securities():
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    try:
        return await sync_taiwan_securities()
    except Exception as exc:
        raise HTTPException(503, f"TWSE/TPEx sync failed: {exc}") from exc


@app.get("/api/health")
async def health():
    return {
        "api": True,
        "postgres": postgres_health(),
        "llm": await llm_health(),
        "llm_provider": get_settings().llm_provider,
    }
