from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import auth
from app.db import get_db
from app.events import log_event

router = APIRouter(prefix="/api/auth", tags=["auth"])


class PasswordIn(BaseModel):
    password: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str


def _require_csrf(request: Request) -> None:
    if request.headers.get(auth.CSRF_HEADER) != "1":
        raise HTTPException(403, "Missing CSRF header")


@router.get("/state")
def state(request: Request, db: Session = Depends(get_db)) -> dict:
    logged_in = True
    try:
        auth.require_owner(request, db)
    except HTTPException:
        logged_in = False
    return {
        "setup_done": auth.is_setup_done(db),
        "logged_in": logged_in,
        "via_tailscale": auth.is_tailscale_owner(request),
    }


@router.post("/setup", dependencies=[Depends(_require_csrf)])
def setup(body: PasswordIn, response: Response, db: Session = Depends(get_db)) -> dict:
    if auth.is_setup_done(db):
        raise HTTPException(409, "Already set up")
    auth.set_password(db, body.password)
    log_event(db, "owner", "auth.setup")
    db.commit()
    auth.issue_session(db, response)
    return {"ok": True}


@router.post("/login", dependencies=[Depends(_require_csrf)])
def login(body: PasswordIn, response: Response, db: Session = Depends(get_db)) -> dict:
    if not auth.check_password(db, body.password):
        log_event(db, "system", "auth.login_failed")
        db.commit()
        raise HTTPException(401, "Wrong password")
    auth.issue_session(db, response)
    return {"ok": True}


@router.post("/logout", dependencies=[Depends(_require_csrf)])
def logout(response: Response) -> dict:
    auth.clear_session(response)
    return {"ok": True}


@router.post("/change-password", dependencies=[Depends(auth.require_owner)])
def change_password(
    body: ChangePasswordIn, response: Response, db: Session = Depends(get_db)
) -> dict:
    if not auth.check_password(db, body.current_password):
        raise HTTPException(401, "Wrong current password")
    auth.set_password(db, body.new_password)
    log_event(db, "owner", "auth.password_changed")
    db.commit()
    auth.issue_session(db, response)
    return {"ok": True}
