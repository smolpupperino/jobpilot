from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.db import get_db, utcnow
from app.models import JOB_STATUSES, Event, Fact, Job, Resume
from app.settings_store import AutomationSettings, get_setting

router = APIRouter(prefix="/api/overview", tags=["overview"], dependencies=[Depends(require_owner)])


def summary(db: Session, hours: int) -> dict:
    """Counts used by the dashboard AND the Telegram bot, so both always agree."""
    since = utcnow() - timedelta(hours=hours)
    by_status = dict(
        db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    )
    new_jobs = db.scalar(
        select(func.count()).select_from(Job).where(Job.created_at >= since)
    )
    status_changes = db.execute(
        select(Event.data).where(Event.type == "job.status_changed", Event.ts >= since)
    ).scalars().all()
    moved_to: dict[str, int] = {}
    for d in status_changes:
        after = d.get("after")
        if after:
            moved_to[after] = moved_to.get(after, 0) + 1
    unconfirmed = db.scalar(
        select(func.count()).select_from(Fact).where(Fact.status == "unconfirmed")
    )
    active_resume = db.scalar(select(Resume).where(Resume.is_active.is_(True)))
    automation = get_setting(db, "automation", AutomationSettings)
    return {
        "window_hours": hours,
        "jobs_by_status": {s: by_status.get(s, 0) for s in JOB_STATUSES},
        "jobs_total": sum(by_status.values()),
        "new_jobs_in_window": new_jobs,
        "status_changes_in_window": moved_to,
        "waiting_for_owner": by_status.get("waiting_for_owner", 0),
        "unconfirmed_facts": unconfirmed,
        "active_resume_version": active_resume.version if active_resume else None,
        "automation_paused": automation.paused,
        "dry_run": automation.dry_run,
    }


@router.get("")
def overview(hours: int = Query(default=24, ge=1, le=24 * 90), db: Session = Depends(get_db)):
    data = summary(db, hours)
    recent = db.scalars(select(Event).order_by(Event.id.desc()).limit(30)).all()
    data["recent_events"] = [
        {"id": e.id, "ts": e.ts, "actor": e.actor, "type": e.type, "job_id": e.job_id,
         "data": e.data}
        for e in recent
    ]
    return data
