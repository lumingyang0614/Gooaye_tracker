from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import init_db, postgres_health
from app.services.dashboard import build_dashboard, episodes_page
from app.services.llm import llm_health
from app.services.sync import sync_gooaye

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


@app.post("/api/sync")
async def sync(limit: int | None = None):
    if not postgres_health():
        raise HTTPException(503, "PostgreSQL unavailable")
    return await sync_gooaye(limit=limit)


@app.get("/api/health")
async def health():
    return {
        "api": True,
        "postgres": postgres_health(),
        "llm": await llm_health(),
        "llm_provider": get_settings().llm_provider,
    }
