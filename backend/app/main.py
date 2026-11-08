import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api import (
    accounts_routes,
    apply_routes,
    auth_routes,
    jobs_routes,
    overview_routes,
    profile_routes,
    questions_routes,
    resume_routes,
    settings_routes,
)
from app.db import get_engine
from app.telegram.bot import run_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx logs every request URL at INFO, and Telegram puts the bot token in the URL.
# Keep these quiet so secrets never reach the container logs.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.apply.worker import recover_interrupted

    recover_interrupted()
    bot_task = asyncio.create_task(run_bot())
    yield
    bot_task.cancel()


def create_app(start_background: bool = True) -> FastAPI:
    app = FastAPI(
        title="JobPilot",
        lifespan=lifespan if start_background else None,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/api/health")
    def health() -> dict:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return {"ok": True}

    for r in (
        auth_routes,
        profile_routes,
        jobs_routes,
        resume_routes,
        overview_routes,
        settings_routes,
        questions_routes,
        apply_routes,
        accounts_routes,
    ):
        app.include_router(r.router)

    @app.exception_handler(404)
    async def not_found(request, exc):
        # Unknown /api paths get JSON; everything else falls back to the SPA.
        if request.url.path.startswith("/api/") or not (STATIC_DIR / "index.html").exists():
            detail = getattr(exc, "detail", "Not found")
            return JSONResponse({"detail": detail}, status_code=404)
        return FileResponse(STATIC_DIR / "index.html")

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
