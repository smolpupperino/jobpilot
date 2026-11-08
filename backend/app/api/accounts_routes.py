"""Job-site accounts JobPilot created, and the Gmail connection used for verification."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.config import get_settings
from app.db import get_db
from app.events import log_event
from app.models import SiteAccount
from app.vault import VaultError, password_of, shared_password

router = APIRouter(prefix="/api", tags=["accounts"], dependencies=[Depends(require_owner)])


class AccountOut(BaseModel):
    id: int
    site: str
    login_email: str
    status: str
    created_at: datetime
    last_used_at: datetime | None


@router.get("/accounts")
def list_accounts(db: Session = Depends(get_db)) -> list[AccountOut]:
    rows = db.scalars(select(SiteAccount).order_by(SiteAccount.id.desc())).all()
    return [AccountOut.model_validate(a, from_attributes=True) for a in rows]


@router.post("/accounts/password")
def reveal_password(db: Session = Depends(get_db)) -> dict:
    """The one shared job-site password (so the owner can log in themselves)."""
    try:
        pw = shared_password(db)
    except VaultError as e:
        raise HTTPException(500, str(e)) from e
    log_event(db, "owner", "account.password_revealed")
    db.commit()
    return {"password": pw}


@router.post("/accounts/{account_id}/password")
def reveal_one(account_id: int, db: Session = Depends(get_db)) -> dict:
    a = db.get(SiteAccount, account_id)
    if a is None:
        raise HTTPException(404, "Not found")
    log_event(db, "owner", "account.password_revealed", site=a.site)
    db.commit()
    return {"password": password_of(a)}


@router.get("/gmail/status")
async def gmail_status() -> dict:
    from app import mail

    s = get_settings()
    out = {"mode": mail.mode(), "address": s.gmail_address or None, "signed_in": None}
    if out["mode"] == "browser":
        from app.apply.browser import cdp

        try:
            async with cdp() as (_b, ctx):
                out["signed_in"] = await mail.gmail_browser_status(ctx)
        except Exception as e:  # noqa: BLE001
            out["error"] = f"Browser not reachable ({type(e).__name__})"
    return out


@router.post("/gmail/open")
async def gmail_open() -> dict:
    """Open Gmail in JobPilot's browser so the owner can sign in (via the viewer)."""
    from app.apply.browser import cdp

    async with cdp() as (_b, ctx):
        page = await ctx.new_page()
        await page.goto("https://mail.google.com/mail/u/0/", wait_until="domcontentloaded", timeout=60000)
        await page.bring_to_front()
    return {"ok": True, "view_url": get_settings().browser_view_url or None}
