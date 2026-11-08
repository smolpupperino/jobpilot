"""About Me profile logic, shared by the dashboard API and the Telegram bot."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.events import log_event
from app.models import FACT_SECTIONS, FACT_SOURCES, FACT_STATUSES, SENSITIVITY, Fact, FactVersion

# Values that must never be stored in the profile, whatever the label says.
_SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
_FORBIDDEN_KEY_WORDS = ("ssn", "social security", "password", "passport", "bank", "routing")


class ProfileError(ValueError):
    pass


def normalize_key(text: str) -> str:
    """Stable key for a question/label: lowercase, punctuation stripped, spaces collapsed."""
    t = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", t).strip()[:300]


def _guard(label: str, value: str | None) -> None:
    lowered = label.lower()
    if any(w in lowered for w in _FORBIDDEN_KEY_WORDS):
        raise ProfileError(
            "That kind of information (SSN, passwords, bank or passport numbers) is never "
            "stored in JobPilot. Handle that question manually."
        )
    if value and _SSN_RE.search(value):
        raise ProfileError("That value looks like a Social Security number. Not stored.")


def upsert_fact(
    db: Session,
    *,
    section: str,
    label: str,
    value: str | None,
    source: str,
    status: str,
    key: str | None = None,
    sensitivity: str = "normal",
    actor: str = "owner",
) -> Fact:
    if section not in FACT_SECTIONS:
        raise ProfileError(f"Unknown section: {section}")
    if source not in FACT_SOURCES:
        raise ProfileError(f"Unknown source: {source}")
    if status not in FACT_STATUSES:
        raise ProfileError(f"Unknown status: {status}")
    if sensitivity not in SENSITIVITY:
        raise ProfileError(f"Unknown sensitivity: {sensitivity}")
    label = label.strip()
    if not label:
        raise ProfileError("Label is required")
    _guard(label, value)

    key = normalize_key(key or label)
    fact = db.scalar(select(Fact).where(Fact.section == section, Fact.key == key))
    created = fact is None
    if fact is None:
        fact = Fact(section=section, key=key, label=label)
        db.add(fact)
    fact.label = label
    fact.value = value
    fact.source = source
    fact.status = status
    fact.sensitivity = sensitivity
    fact.confirmed_at = utcnow() if status == "confirmed" else None
    db.flush()
    db.add(FactVersion(fact_id=fact.id, value=value, source=source, status=status))
    log_event(
        db,
        actor,
        "profile.fact_created" if created else "profile.fact_updated",
        fact_id=fact.id,
        section=section,
        label=label,
    )
    return fact


def delete_fact(db: Session, fact: Fact, actor: str = "owner") -> None:
    log_event(db, actor, "profile.fact_deleted", fact_id=fact.id, section=fact.section,
              label=fact.label)
    db.delete(fact)
