from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.db import get_db
from app.events import log_event
from app.jobs_util import InvalidJobURL, canonicalize_url, detect_ats
from app.models import JOB_STATUSES, Event, Job

router = APIRouter(prefix="/api/jobs", tags=["jobs"], dependencies=[Depends(require_owner)])


class JobOut(BaseModel):
    id: int
    url: str
    employer: str
    title: str
    location: str
    ats: str
    source: str
    status: str
    notes: str
    created_at: datetime
    updated_at: datetime


class JobIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    employer: str = Field(default="", max_length=300)
    title: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=300)
    notes: str = Field(default="", max_length=5000)


class JobPatch(BaseModel):
    employer: str | None = Field(default=None, max_length=300)
    title: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=300)
    status: str | None = None
    notes: str | None = Field(default=None, max_length=5000)


class EventOut(BaseModel):
    id: int
    ts: datetime
    actor: str
    type: str
    data: dict


def _out(j: Job) -> JobOut:
    return JobOut.model_validate(j, from_attributes=True)


@router.get("/statuses")
def statuses() -> list[str]:
    return list(JOB_STATUSES)


@router.get("")
def list_jobs(
    status: str | None = Query(default=None), db: Session = Depends(get_db)
) -> list[JobOut]:
    q = select(Job).order_by(Job.updated_at.desc())
    if status:
        q = q.where(Job.status == status)
    return [_out(j) for j in db.scalars(q).all()]


@router.post("")
def create_job(body: JobIn, db: Session = Depends(get_db)) -> JobOut:
    try:
        canon = canonicalize_url(body.url)
    except InvalidJobURL as e:
        raise HTTPException(400, str(e)) from e
    existing = db.scalar(select(Job).where(Job.canonical_url == canon))
    if existing:
        raise HTTPException(409, {"message": "Already tracked", "job_id": existing.id})
    job = Job(
        url=body.url.strip(),
        canonical_url=canon,
        employer=body.employer.strip(),
        title=body.title.strip(),
        location=body.location.strip(),
        notes=body.notes,
        ats=detect_ats(canon),
        source="manual",
    )
    db.add(job)
    db.flush()
    log_event(db, "owner", "job.added", job_id=job.id, ats=job.ats, source="manual")
    db.commit()
    return _out(job)


@router.get("/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Not found")
    evs = db.scalars(select(Event).where(Event.job_id == job_id).order_by(Event.ts)).all()
    return {
        "job": _out(job),
        "events": [EventOut.model_validate(e, from_attributes=True) for e in evs],
    }


@router.patch("/{job_id}")
def update_job(job_id: int, body: JobPatch, db: Session = Depends(get_db)) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Not found")
    changes = body.model_dump(exclude_none=True)
    if "status" in changes and changes["status"] not in JOB_STATUSES:
        raise HTTPException(400, "Unknown status")
    before_status = job.status
    for k, v in changes.items():
        setattr(job, k, v.strip() if isinstance(v, str) and k != "notes" else v)
    if "status" in changes and changes["status"] != before_status:
        log_event(db, "owner", "job.status_changed", job_id=job.id,
                  before=before_status, after=job.status)
    elif changes:
        log_event(db, "owner", "job.edited", job_id=job.id, fields=sorted(changes))
    db.commit()
    return _out(job)


@router.delete("/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_db)) -> dict:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "Not found")
    log_event(db, "owner", "job.deleted", employer=job.employer, title=job.title)
    db.delete(job)
    db.commit()
    return {"ok": True}
