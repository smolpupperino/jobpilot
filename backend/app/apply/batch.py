"""Batch runs: apply to the next N jobs, one after another, then send one summary.

N is `automation.batch_size` (Settings, or Telegram /batch N). Only submitted applications
count toward N: jobs skipped by the fit check or that fail are reported but don't use up a
slot. To avoid burning through every lead, a batch tries at most 3*N jobs.

Which jobs: status "interested" first, then "new", newest first. Never LinkedIn URLs (no
LinkedIn automation; those leads stay for applying by hand), and never a job whose last
attempt ended in an error or ran out of steps (retry those from the Jobs page).

Stops early on: dashboard Stop, Pause (dashboard or Telegram /pause), or running out of jobs.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import select

from app.apply import worker
from app.db import session_factory, utcnow
from app.events import log_event
from app.models import Event, Job
from app.settings_store import AutomationSettings, get_setting
from app.telegram.bot import notify

log = logging.getLogger("jobpilot.batch")

PICK_STATUSES = ("interested", "new")
NO_AUTO_ATS = ("linkedin",)
FAILED_OUTCOMES = ("error", "too_many_steps")
MAX_TRIES_FACTOR = 3

_state: dict[str, Any] = {"running": False, "target": 0, "submitted": 0, "tried": 0,
                          "current": None, "started_at": None}
_results: list[dict] = []
_stop = asyncio.Event()
_task: asyncio.Task | None = None


def status() -> dict[str, Any]:
    return {**_state, "results": list(_results)}


def _db():
    return session_factory()()


def _automation() -> AutomationSettings:
    with _db() as db:
        return get_setting(db, "automation", AutomationSettings)


def next_job(exclude: set[int]) -> int | None:
    """The next job a batch should apply to, or None when there are none left."""
    with _db() as db:
        last: dict[int, str] = {}
        for e in db.scalars(select(Event).where(Event.type == "apply.finished").order_by(Event.id)).all():
            if e.job_id is not None:
                last[e.job_id] = (e.data or {}).get("outcome", "")
        failed = {j for j, o in last.items() if o in FAILED_OUTCOMES}
        for status in PICK_STATUSES:
            rows = db.scalars(select(Job).where(Job.status == status, Job.ats.not_in(NO_AUTO_ATS))
                              .order_by(Job.created_at.desc(), Job.id.desc())).all()
            for j in rows:
                if j.id not in exclude and j.id not in failed:
                    return j.id
    return None


def _job_label(job_id: int) -> str:
    with _db() as db:
        j = db.get(Job, job_id)
        if j is None:
            return f"job #{job_id}"
        return f"{j.title or 'Job'} at {j.employer or j.url[:60]}"


def _fit_score(job_id: int) -> int | None:
    with _db() as db:
        e = db.scalar(select(Event).where(Event.job_id == job_id, Event.type == "apply.fit")
                      .order_by(Event.id.desc()))
        return (e.data or {}).get("score") if e else None


def summary_text(target: int, results: list[dict], ended: str) -> str:
    submitted = [r for r in results if r["submitted"]]
    lines = [f"Batch done: applied to {len(submitted)} of {target}. {ended}"]
    groups = [
        ("Submitted and confirmed", lambda r: r["outcome"] == "confirmed"),
        ("Submitted, no confirmation page seen (check these)", lambda r: r["submitted"]
         and r["outcome"] != "confirmed"),
        ("Already applied before", lambda r: r["outcome"] == "already_applied" and not r["submitted"]),
        ("Skipped (poor fit)", lambda r: r["outcome"] == "skipped_fit"),
        ("Stopped", lambda r: r["outcome"] == "stopped" and not r["submitted"]),
        ("Failed (retry from the Jobs page)",
         lambda r: r["outcome"] in FAILED_OUTCOMES and not r["submitted"]),
    ]
    for title, match in groups:
        rows = [r for r in results if match(r)]
        if rows:
            lines += ["", f"{title}:"]
            for r in rows:
                fit = r.get("fit") if r["outcome"] == "skipped_fit" else None
                extra = f" (fit {fit})" if fit is not None else ""
                lines.append(f"- {r['label']}{extra}")
    return "\n".join(lines)


async def _run(target: int) -> None:
    tried: set[int] = set()
    ended = "Out of jobs to apply to."
    while _state["submitted"] < target:
        if _stop.is_set():
            ended = "Stopped from the dashboard."
            break
        if _automation().paused:
            ended = "Stopped: automation was paused."
            break
        if len(tried) >= target * MAX_TRIES_FACTOR:
            ended = f"Stopped after trying {len(tried)} jobs."
            break
        job_id = next_job(tried)
        if job_id is None:
            break
        tried.add(job_id)
        _state.update(current=job_id, tried=len(tried))
        try:
            await worker.start(job_id)
        except worker.ApplyError as e:
            # Can't start at all (paused, AI runner down, no resume): stop the batch.
            ended = f"Stopped: {e}"
            break
        await worker.wait()
        res = worker.last_result()
        r = {"job_id": job_id, "label": _job_label(job_id), "outcome": res.get("outcome", "error"),
             "submitted": bool(res.get("submitted"))}
        if r["outcome"] == "skipped_fit":
            r["fit"] = _fit_score(job_id)
        _results.append(r)
        if r["submitted"]:
            _state["submitted"] += 1
    else:
        ended = ""
    with _db() as db:
        log_event(db, "system", "batch.finished", target=target, submitted=_state["submitted"],
                  tried=len(tried), ended=ended)
        db.commit()
    notify(summary_text(target, _results, ended).strip())


async def _guarded(target: int) -> None:
    try:
        await _run(target)
    except Exception:  # noqa: BLE001
        log.exception("batch failed")
        notify("The batch run crashed. Check the dashboard.")
    finally:
        _state.update(running=False, current=None)


async def start(n: int | None = None) -> dict[str, Any]:
    """Start a batch of n (default: the saved batch size). Raises worker.ApplyError."""
    global _task
    if _state["running"]:
        raise worker.ApplyError("A batch is already running")
    if worker.status()["running"]:
        raise worker.ApplyError("An application is already running")
    a = _automation()
    if a.paused:
        raise worker.ApplyError("Automation is paused. Unpause it first.")
    target = n or a.batch_size
    if next_job(set()) is None:
        raise worker.ApplyError("No jobs waiting to apply to (status New or Interested).")
    _stop.clear()
    _results.clear()
    _state.update(running=True, target=target, submitted=0, tried=0, current=None,
                  started_at=utcnow().isoformat())
    with _db() as db:
        log_event(db, "system", "batch.started", target=target)
        db.commit()
    notify(f"Starting a batch: applying to the next {target} jobs. I'll send a summary at the end.")
    _task = asyncio.create_task(_guarded(target))
    return status()


def stop() -> None:
    """Stop the batch and the application in progress."""
    _stop.set()
    worker.stop()
