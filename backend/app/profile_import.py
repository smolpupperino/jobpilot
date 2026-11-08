"""Fill the About Me profile from the owner's own resume.

The AI extracts facts that are literally on the resume (no guessing). They're saved
as confirmed with source "resume_import" (it's the owner's own document), and the
owner can edit or delete any of them on the About Me page. Existing facts are
never overwritten.
"""

from typing import Any

from sqlalchemy import select

from app.ai.client import run_task
from app.apply.fit import resume_text
from app.db import session_factory
from app.models import FACT_SECTIONS, Fact
from app.profile import ProfileError, normalize_key, upsert_fact

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
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
        }
    },
}

SYSTEM = """Extract profile facts from a resume, for filling out job applications. Only facts \
literally stated in the resume; never infer or guess. Use short, form-style labels. Sections:
- contact: "First name", "Last name", "Full name", "Email", "Phone", "City", "State", \
"ZIP code", "Country", "LinkedIn URL", "Website" (only those present).
- work_history: per job N (1 = most recent): "Job N title", "Job N employer", "Job N location", \
"Job N start date" (as written, e.g. "06/2024"), "Job N end date" ("Present" if current), \
"Job N description" (a short summary of the bullets).
- education: per school N: "School N name", "School N degree", "School N field of study", \
"School N start date", "School N end/expected date", "School N status" (e.g. "In progress").
- certifications: one fact per certification: label = cert name, value = issuer/date if given, \
else "Yes".
- skills: "Technical skills" (comma-separated list), plus any notable tools lists.
Do not output anything about SSNs, birth dates, or other sensitive identifiers."""


async def import_from_resume() -> dict:
    text = resume_text()
    if not text.strip():
        raise ValueError("No active resume (or no text could be read from it)")
    res = await run_task("form_mapper", SYSTEM, "RESUME\n" + text, SCHEMA, timeout=300)
    added, skipped = [], 0
    with session_factory()() as db:
        for f in (res.output.get("facts") or [])[:150]:
            section, label = str(f.get("section")), str(f.get("label", "")).strip()
            value = str(f.get("value", ""))
            if section not in FACT_SECTIONS or not label or not value.strip():
                continue
            exists = db.scalar(select(Fact).where(Fact.section == section, Fact.key == normalize_key(label)))
            if exists:
                skipped += 1
                continue
            try:
                upsert_fact(db, section=section, label=label[:300], value=value[:2000],
                            source="resume_import", status="confirmed", actor="system")
                added.append(f"{section}: {label}")
            except ProfileError:
                skipped += 1
        db.commit()
    return {"added": len(added), "skipped_existing": skipped, "labels": added}
