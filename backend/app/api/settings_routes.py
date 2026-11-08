from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.config import get_settings
from app.db import get_db
from app.events import log_event
from app.settings_store import (
    AI_TASKS,
    RUNNABLE_PROVIDERS,
    AISettings,
    AutomationSettings,
    effort_choices,
    get_setting,
    model_choices,
    put_setting,
)
from app.telegram.bot import bot_status

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_owner)])


@router.get("/ai")
def get_ai(db: Session = Depends(get_db)) -> dict:
    return {
        "settings": get_setting(db, "ai", AISettings),
        "tasks": AI_TASKS,
        "model_suggestions": model_choices(),
        "effort_choices": effort_choices(),
        "providers": RUNNABLE_PROVIDERS,
    }


@router.put("/ai")
def put_ai(body: AISettings, db: Session = Depends(get_db)) -> AISettings:
    unknown = set(body.tasks) - set(AI_TASKS)
    if unknown:
        raise HTTPException(400, f"Unknown tasks: {sorted(unknown)}")
    for name, r in body.tasks.items():
        for p in (r.provider, r.fallback_provider):
            if p not in RUNNABLE_PROVIDERS:
                raise HTTPException(400, f"{name}: {p} isn't installed on the mini PC (use claude or codex)")
    # Changes take effect on the NEXT AI call, including mid-application:
    # AI calls are stateless; application progress lives in the DB + browser.
    put_setting(db, "ai", body)
    log_event(db, "owner", "settings.ai_changed",
              routes={k: f"{v.provider}:{v.model}" for k, v in body.tasks.items()})
    db.commit()
    return body


@router.get("/automation")
def get_automation(db: Session = Depends(get_db)) -> AutomationSettings:
    return get_setting(db, "automation", AutomationSettings)


@router.put("/automation")
def put_automation(body: AutomationSettings, db: Session = Depends(get_db)) -> AutomationSettings:
    before = get_setting(db, "automation", AutomationSettings)
    put_setting(db, "automation", body)
    log_event(db, "owner", "settings.automation_changed",
              before=before.model_dump(), after=body.model_dump())
    db.commit()
    return body


@router.get("/telegram")
def telegram() -> dict:
    s = get_settings()
    return {
        "token_configured": bool(s.telegram_bot_token),
        "owner_id_configured": s.telegram_owner_id is not None,
        **bot_status(),
    }


# --- Gemini sign-in (done from Settings; the CLI runs on the host via the AI runner) ---

class GeminiCode(BaseModel):
    code: str = Field(min_length=4, max_length=500)


@router.get("/gemini")
async def gemini_status() -> dict:
    from app.ai.client import runner_request

    h = await runner_request("GET", "/v1/health", timeout=10)
    return {"installed": bool((h.get("providers") or {}).get("gemini")),
            "signed_in": bool(h.get("gemini_signed_in")), "runner_ok": h.get("ok") is True,
            "models": model_choices().get("gemini", [])}


@router.post("/gemini/start")
async def gemini_start() -> dict:
    """Start Google sign-in for Gemini; returns the link to open."""
    from app.ai.client import runner_request

    r = await runner_request("POST", "/v1/gemini/login/start", {}, timeout=90)
    if not r.get("ok"):
        raise HTTPException(502, r.get("error") or "Couldn't start the Gemini sign-in")
    return {"url": r["url"]}


@router.post("/gemini/finish")
async def gemini_finish(body: GeminiCode, db: Session = Depends(get_db)) -> dict:
    """Send the code Google showed; on success, load Gemini's model list."""
    from app.ai.client import runner_request
    from app.settings_store import save_gemini_models

    r = await runner_request("POST", "/v1/gemini/login/finish", {"code": body.code.strip()}, timeout=60)
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "Sign-in failed")
    log_event(db, "owner", "settings.gemini_connected")
    db.commit()
    m = await runner_request("GET", "/v1/gemini/models", timeout=90)
    if m.get("ok") and m.get("models"):
        save_gemini_models(m["models"])
    return await gemini_status()
