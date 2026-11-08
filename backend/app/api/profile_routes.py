from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.db import get_db
from app.models import FACT_SECTIONS, Fact
from app.profile import ProfileError, delete_fact, upsert_fact

router = APIRouter(prefix="/api/profile", tags=["profile"], dependencies=[Depends(require_owner)])


class FactOut(BaseModel):
    id: int
    section: str
    key: str
    label: str
    value: str | None
    source: str
    status: str
    sensitivity: str
    confirmed_at: datetime | None
    used_count: int
    updated_at: datetime


class FactVersionOut(BaseModel):
    value: str | None
    source: str
    status: str
    changed_at: datetime


class FactIn(BaseModel):
    section: str
    label: str = Field(min_length=1, max_length=500)
    value: str | None = Field(default=None, max_length=5000)
    status: str = "confirmed"
    sensitivity: str = "normal"


class FactPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=500)
    value: str | None = Field(default=None, max_length=5000)
    status: str | None = None
    sensitivity: str | None = None


def _out(f: Fact) -> FactOut:
    return FactOut.model_validate(f, from_attributes=True)


@router.get("/sections")
def sections() -> list[str]:
    return list(FACT_SECTIONS)


@router.get("/facts")
def list_facts(db: Session = Depends(get_db)) -> list[FactOut]:
    rows = db.scalars(select(Fact).order_by(Fact.section, Fact.label)).all()
    return [_out(f) for f in rows]


@router.post("/facts")
def create_fact(body: FactIn, db: Session = Depends(get_db)) -> FactOut:
    try:
        f = upsert_fact(
            db,
            section=body.section,
            label=body.label,
            value=body.value,
            source="dashboard",
            status=body.status,
            sensitivity=body.sensitivity,
        )
    except ProfileError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _out(f)


@router.patch("/facts/{fact_id}")
def update_fact(fact_id: int, body: FactPatch, db: Session = Depends(get_db)) -> FactOut:
    f = db.get(Fact, fact_id)
    if f is None:
        raise HTTPException(404, "Not found")
    try:
        f = upsert_fact(
            db,
            section=f.section,
            key=f.key,
            label=body.label if body.label is not None else f.label,
            value=body.value if body.value is not None else f.value,
            source="dashboard",
            status=body.status or f.status,
            sensitivity=body.sensitivity or f.sensitivity,
        )
    except ProfileError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return _out(f)


@router.get("/facts/{fact_id}/history")
def fact_history(fact_id: int, db: Session = Depends(get_db)) -> list[FactVersionOut]:
    f = db.get(Fact, fact_id)
    if f is None:
        raise HTTPException(404, "Not found")
    return [FactVersionOut.model_validate(v, from_attributes=True) for v in f.versions]


@router.delete("/facts/{fact_id}")
def remove_fact(fact_id: int, db: Session = Depends(get_db)) -> dict:
    f = db.get(Fact, fact_id)
    if f is None:
        raise HTTPException(404, "Not found")
    delete_fact(db, f)
    db.commit()
    return {"ok": True}


class CommonOut(BaseModel):
    id: str
    section: str
    label: str
    group: str
    options: list[str]
    help: str
    suggested: str
    value: str | None


class CommonIn(BaseModel):
    answers: dict[str, str] = Field(max_length=100)


@router.get("/common")
def common_questions(db: Session = Depends(get_db)) -> list[CommonOut]:
    from app.common_info import COMMON, find

    out = []
    for q in COMMON:
        f = find(db, q)
        out.append(CommonOut(id=q.id, section=q.section, label=q.label, group=q.group, options=q.options,
                             help=q.help, suggested=q.suggested, value=f.value if f else None))
    return out


@router.post("/common")
def save_common(body: CommonIn, db: Session = Depends(get_db)) -> dict:
    from app.common_info import BY_ID

    saved = 0
    for qid, value in body.answers.items():
        q = BY_ID.get(qid)
        value = (value or "").strip()[:500]
        if q is None or not value:
            continue
        if q.options and value not in q.options:
            raise HTTPException(400, f"{q.label}: pick one of the options")
        try:
            upsert_fact(db, section=q.section, label=q.label, value=value, source="dashboard",
                        status="confirmed")
        except ProfileError as e:
            raise HTTPException(400, f"{q.label}: {e}") from e
        saved += 1
    db.commit()
    return {"saved": saved}


@router.post("/import-resume")
async def import_resume() -> dict:
    """Fill About Me from the active resume (adds only facts that don't exist yet)."""
    from app.ai.client import AIUnavailable
    from app.profile_import import import_from_resume

    try:
        return await import_from_resume()
    except (ValueError, AIUnavailable) as e:
        raise HTTPException(400, str(e)) from e
