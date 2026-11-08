"""Owner questions: JobPilot asks, the owner answers (Telegram or dashboard),
and the answer is saved to the About Me profile."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.events import log_event
from app.models import OwnerQuestion
from app.profile import upsert_fact


def create_question(
    db: Session,
    question: str,
    options: list[str] | None = None,
    job_id: int | None = None,
    section: str = "answers",
    kind: str = "fact",
    photo_path: str | None = None,
) -> OwnerQuestion:
    q = OwnerQuestion(
        question=question.strip()[:2000],
        options=[o[:100] for o in (options or [])][:8],
        job_id=job_id,
        section=section,
        kind=kind,
        photo_path=photo_path,
    )
    db.add(q)
    db.flush()
    log_event(db, "system", "question.created", job_id=job_id, question_id=q.id,
              question=q.question)
    return q


def latest_open(db: Session) -> OwnerQuestion | None:
    """Latest open question a typed reply can answer: profile questions and "stuck"
    (takeover) questions. Never approvals: Submit must be a button tap."""
    return db.scalar(
        select(OwnerQuestion)
        .where(OwnerQuestion.status == "open", OwnerQuestion.kind.in_(["fact", "takeover", "info"]))
        .order_by(OwnerQuestion.id.desc())
        .limit(1)
    )


def answer_question(db: Session, q: OwnerQuestion, answer: str, actor: str) -> OwnerQuestion:
    """Record the answer. For "fact" questions it's also saved as a confirmed profile
    fact (raises ProfileError for forbidden data). Decisions (approval/takeover) aren't."""
    answer = answer.strip()[:5000]
    if q.kind != "fact":
        # Takeover and info questions accept plain language (the apply worker reads it);
        # approvals only their buttons.
        if q.kind not in ("takeover", "info") and q.options and answer not in q.options:
            raise ValueError(f"Pick one of: {', '.join(q.options)}")
        q.status = "answered"
        q.answer = answer
        q.answered_at = utcnow()
        log_event(db, actor, "question.decided", job_id=q.job_id, question_id=q.id, answer=answer)
        return q
    fact = upsert_fact(
        db,
        section=q.section,
        label=q.question,
        value=answer,
        source="telegram" if actor == "telegram" else "dashboard",
        status="confirmed",
        actor=actor,
    )
    q.status = "answered"
    q.answer = answer
    q.fact_id = fact.id
    q.answered_at = utcnow()
    log_event(db, actor, "question.answered", job_id=q.job_id, question_id=q.id)
    return q
