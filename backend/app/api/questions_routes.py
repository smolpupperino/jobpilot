from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.db import get_db
from app.models import OwnerQuestion
from app.profile import ProfileError
from app.questions import answer_question, create_question

router = APIRouter(
    prefix="/api/questions", tags=["questions"], dependencies=[Depends(require_owner)]
)


class QuestionOut(BaseModel):
    id: int
    question: str
    options: list[str]
    status: str
    answer: str | None
    job_id: int | None
    kind: str
    created_at: datetime


class QuestionIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    options: list[str] = Field(default_factory=list, max_length=8)


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=5000)


@router.get("")
def list_questions(db: Session = Depends(get_db)) -> list[QuestionOut]:
    rows = db.scalars(select(OwnerQuestion).order_by(OwnerQuestion.id.desc()).limit(100)).all()
    return [QuestionOut.model_validate(q, from_attributes=True) for q in rows]


@router.post("")
def ask(body: QuestionIn, db: Session = Depends(get_db)) -> QuestionOut:
    """Create a question (used for testing the Telegram flow from the dashboard).
    The Telegram loop sends it within ~30 seconds."""
    q = create_question(db, body.question, body.options)
    db.commit()
    return QuestionOut.model_validate(q, from_attributes=True)


@router.post("/{qid}/answer")
def answer(qid: int, body: AnswerIn, db: Session = Depends(get_db)) -> QuestionOut:
    q = db.get(OwnerQuestion, qid)
    if q is None:
        raise HTTPException(404, "Not found")
    if q.status != "open":
        raise HTTPException(409, "Already answered")
    try:
        answer_question(db, q, body.answer, actor="owner")
    except (ProfileError, ValueError) as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return QuestionOut.model_validate(q, from_attributes=True)
