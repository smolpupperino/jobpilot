"""Read the owner's plain-language reply to a list of questions and save it as facts.

One reply can answer several questions ("12 Oak St, Salem OR 97301" ->
street, city, state, ZIP), refer to earlier answers ("it's in my previous message"),
or be casual ("same as my real name"). The AI turns it into clean About Me facts;
code validates them (known sections, forbidden data rejected by upsert_fact).
"""

import json
import logging
from typing import Any

from sqlalchemy import select

from app.ai.client import AIUnavailable, run_task
from app.common_info import COMMON
from app.db import session_factory
from app.models import FACT_SECTIONS, Fact, OwnerQuestion
from app.profile import ProfileError, upsert_fact

log = logging.getLogger("jobpilot.info")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts", "unanswered"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["section", "label", "value"],
                "properties": {
                    "section": {"type": "string", "enum": list(FACT_SECTIONS)},
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                },
            },
        },
        "unanswered": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """A job-application assistant asked its owner some questions and got one casual reply. \
Turn the reply into profile facts to save.
- One reply may answer several questions. Split combined answers: a full address becomes \
"Street address", "City", "State", "ZIP code" (and "Country" if given or obvious, e.g. a US state).
- Resolve references using the earlier conversation and existing facts ("it's in my previous \
message", "same as my real name", "yes to all", "no to both").
- Use these standard labels and sections when a question matches one: STANDARD_LABELS. Otherwise \
use a short, general form-style label (not tied to one employer) in the best section.
- If a question listed options, use the option the reply means (closest match); a bare "yes"/"no" \
answers a yes/no question. Use a label that states the question plainly.
- Only save what the owner actually said or clearly implied; never guess. List questions the \
reply didn't answer in "unanswered" (as short questions).
- Never output SSNs, passwords, bank details, or passport numbers."""


def _standard_labels() -> str:
    return "; ".join(f"{q.label} [{q.section}]" for q in COMMON)


def _facts() -> list[dict]:
    with session_factory()() as db:
        rows = db.scalars(select(Fact).where(Fact.status == "confirmed")).all()
        return [{"section": f.section, "label": f.label, "value": f.value} for f in rows if f.value][:200]


def _recent_answers(job_id: int | None) -> list[dict]:
    with session_factory()() as db:
        rows = db.scalars(select(OwnerQuestion).where(OwnerQuestion.status == "answered",
                                                      OwnerQuestion.kind.in_(["fact", "info"]))
                          .order_by(OwnerQuestion.id.desc()).limit(6)).all()
        return [{"asked": q.question[:600], "owner_replied": (q.answer or "")[:600]} for q in reversed(rows)]


async def absorb(questions: str, reply: str, job_id: int | None = None) -> dict:
    """Save the facts in `reply`. Returns {"saved": [(label, value)], "unanswered": [...],
    "rejected": [...]}; raises AIUnavailable if no AI could read it."""
    prompt = json.dumps({"questions_asked": questions, "owner_reply": reply,
                         "earlier_conversation": _recent_answers(job_id), "existing_facts": _facts()},
                        ensure_ascii=False)
    out = (await run_task("form_mapper", SYSTEM.replace("STANDARD_LABELS", _standard_labels()), prompt,
                          SCHEMA, job_id=job_id, timeout=150)).output
    saved, rejected = [], []
    with session_factory()() as db:
        for f in (out.get("facts") or [])[:40]:
            section, label = str(f.get("section")), str(f.get("label", "")).strip()[:300]
            value = str(f.get("value", "")).strip()[:2000]
            if section not in FACT_SECTIONS or not label or not value:
                continue
            try:
                upsert_fact(db, section=section, label=label, value=value, source="telegram",
                            status="confirmed", actor="telegram")
                saved.append((label, value))
            except ProfileError as e:
                rejected.append(f"{label}: {e}")
        db.commit()
    unanswered = [str(u)[:200] for u in (out.get("unanswered") or [])][:20]
    if not saved and not rejected:
        one = _single_question(questions)
        if one and 0 < len(reply.strip()) <= 80:
            # One question, short reply, nothing saved: the reply IS the answer to that question.
            value = reply.strip()
            if value.lower() in ("y", "yes", "yeah", "yep", "yup"):
                value = "Yes"
            elif value.lower() in ("n", "no", "nope", "nah"):
                value = "No"
            with session_factory()() as db:
                try:
                    upsert_fact(db, section="answers", label=one[:300], value=value[:2000], source="telegram",
                                status="confirmed", actor="telegram")
                    db.commit()
                    saved.append((one[:300], value))
                    unanswered = []
                except ProfileError as e:
                    rejected.append(f"{one[:60]}: {e}")
    return {"saved": saved, "unanswered": unanswered, "rejected": rejected}


def _single_question(questions: str) -> str | None:
    """The question text if the message asked exactly one ("1. <question> (options: ...)")."""
    import re

    items = re.findall(r"^\s*\d+\.\s+(.+)$", questions, re.M)
    if len(items) != 1:
        return None
    return re.sub(r"\s*\((options|e\.g\.)[^)]*\)?\s*$", "", items[0]).strip() or None


def summary_text(result: dict) -> str:
    lines = []
    if result["saved"]:
        lines.append("Saved to About Me:")
        lines += [f"- {label}: {value}" for label, value in result["saved"]]
    if result["rejected"]:
        lines.append("Not saved: " + "; ".join(result["rejected"]))
    if result["unanswered"]:
        lines.append("Still need: " + "; ".join(result["unanswered"]))
    return "\n".join(lines) or "I couldn't find an answer in that. Could you say it another way?"


__all__ = ["AIUnavailable", "absorb", "summary_text"]
