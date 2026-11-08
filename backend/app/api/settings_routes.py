from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.config import get_settings
from app.db import get_db
from app.events import log_event
from app.settings_store import (
    AI_TASKS,
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
    }


@router.put("/ai")
def put_ai(body: AISettings, db: Session = Depends(get_db)) -> AISettings:
    unknown = set(body.tasks) - set(AI_TASKS)
    if unknown:
        raise HTTPException(400, f"Unknown tasks: {sorted(unknown)}")
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
