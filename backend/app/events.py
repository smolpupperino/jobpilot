"""Append-only activity log helpers."""

from typing import Any

from sqlalchemy.orm import Session

from app.models import Event

# Keys that must never land in event data, even by accident.
_REDACT_KEYS = {"password", "token", "secret", "ssn", "security_answer", "cookie"}


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if any(r in k.lower() for r in _REDACT_KEYS):
            out[k] = "[redacted]"
        elif isinstance(v, dict):
            out[k] = _redact(v)
        else:
            out[k] = v
    return out


def log_event(
    db: Session,
    actor: str,
    type_: str,
    job_id: int | None = None,
    **data: Any,
) -> Event:
    ev = Event(actor=actor, type=type_, job_id=job_id, data=_redact(data))
    db.add(ev)
    return ev
