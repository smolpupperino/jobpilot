"""Database tables.

Kept intentionally small for v1 (see SPEC.md section 2):
- facts:         everything JobPilot knows about the owner ("About Me"), including
                 the application answer library (section = "answers").
- fact_versions: every past value of a fact, so old applications can show what
                 was true at the time.
- jobs:          tracked job postings / applications.
- events:        plain append-only activity log (who did what, when).
- resumes:       uploaded resume PDFs (immutable, hashed).
- settings_kv:   owner-editable settings (AI routing, kill switch, ...).
"""

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

# --- Fact vocab -------------------------------------------------------------

FACT_SECTIONS = (
    "contact",
    "work_history",
    "education",
    "certifications",
    "authorization",
    "preferences",
    "skills",
    "answers",
)
FACT_SOURCES = ("dashboard", "telegram", "resume_import", "seed")
FACT_STATUSES = ("unconfirmed", "confirmed")
SENSITIVITY = ("normal", "sensitive")

# --- Job vocab --------------------------------------------------------------

JOB_STATUSES = (
    "new",
    "interested",
    "skipped",
    "applying",
    "waiting_for_owner",
    "submitted_uncertain",
    "submitted_confirmed",
    "acknowledged",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
    "closed",
)
JOB_SOURCES = ("manual", "linkedin_alert", "feed", "email")


class Fact(Base):
    __tablename__ = "facts"
    __table_args__ = (UniqueConstraint("section", "key", name="uq_fact_section_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(String(32), index=True)
    # Stable machine key, e.g. "first_name" or a normalized question text.
    key: Mapped[str] = mapped_column(String(300))
    # Human label / the question exactly as first seen.
    label: Mapped[str] = mapped_column(String(500))
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="dashboard")
    status: Mapped[str] = mapped_column(String(16), default="unconfirmed")
    sensitivity: Mapped[str] = mapped_column(String(16), default="normal")
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    versions: Mapped[list["FactVersion"]] = relationship(
        back_populates="fact", cascade="all, delete-orphan", order_by="FactVersion.id"
    )


class FactVersion(Base):
    __tablename__ = "fact_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fact_id: Mapped[int] = mapped_column(ForeignKey("facts.id", ondelete="CASCADE"), index=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    changed_at: Mapped[datetime] = mapped_column(default=utcnow)

    fact: Mapped[Fact] = relationship(back_populates="versions")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(2000))
    canonical_url: Mapped[str] = mapped_column(String(2000), unique=True)
    employer: Mapped[str] = mapped_column(String(300), default="")
    title: Mapped[str] = mapped_column(String(300), default="")
    location: Mapped[str] = mapped_column(String(300), default="")
    ats: Mapped[str] = mapped_column(String(32), default="unknown")
    source: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(16))  # owner | system | telegram | worker | ai
    type: Mapped[str] = mapped_column(String(64), index=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    filename: Mapped[str] = mapped_column(String(300))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(default=False)
    uploaded_at: Mapped[datetime] = mapped_column(default=utcnow)


class OwnerQuestion(Base):
    """A question JobPilot needs the owner to answer (sent via Telegram).
    The answer is saved to the profile's answer library, so it's asked only once."""

    __tablename__ = "owner_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    question: Mapped[str] = mapped_column(String(2000))
    options: Mapped[list] = mapped_column(JSON, default=list)
    section: Mapped[str] = mapped_column(String(32), default="answers")
    # "fact": the answer is saved to the profile. "approval" / "takeover": a decision
    # for a running application (Submit/Cancel, Continue/Cancel); not saved as a fact.
    kind: Mapped[str] = mapped_column(String(16), default="fact", server_default="fact")
    # Optional screenshot sent along with the question (path under data_dir).
    photo_path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    fact_id: Mapped[int | None] = mapped_column(
        ForeignKey("facts.id", ondelete="SET NULL"), nullable=True
    )
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SiteAccount(Base):
    """A login JobPilot created on a job site. Password is AES-GCM encrypted (app/vault.py)."""

    __tablename__ = "site_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site: Mapped[str] = mapped_column(String(255), unique=True)
    login_email: Mapped[str] = mapped_column(String(255))
    password_enc: Mapped[str] = mapped_column(Text)
    # creating -> active (signed in at least once); needs_attention on repeated failures
    status: Mapped[str] = mapped_column(String(32), default="creating")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(nullable=True)


class SettingKV(Base):
    __tablename__ = "settings_kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
