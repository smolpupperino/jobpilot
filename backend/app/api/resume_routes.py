"""Resume storage. The owner's PDF is stored unchanged, read-only, and hashed.
Every upload is a new version; nothing is ever overwritten."""

import hashlib
import io
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from pypdf import PdfReader
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth import require_owner
from app.config import get_settings
from app.db import get_db
from app.events import log_event
from app.models import Resume

router = APIRouter(prefix="/api/resumes", tags=["resumes"], dependencies=[Depends(require_owner)])

MAX_BYTES = 5 * 1024 * 1024


class ResumeOut(BaseModel):
    id: int
    version: int
    filename: str
    sha256: str
    size_bytes: int
    is_active: bool
    uploaded_at: datetime


def _path(r: Resume) -> str:
    return str(get_settings().resume_dir / f"v{r.version}-{r.sha256[:12]}.pdf")


@router.get("")
def list_resumes(db: Session = Depends(get_db)) -> list[ResumeOut]:
    rows = db.scalars(select(Resume).order_by(Resume.version.desc())).all()
    return [ResumeOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("")
async def upload_resume(file: UploadFile, db: Session = Depends(get_db)) -> ResumeOut:
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "Resume must be under 5 MB")
    if not data.startswith(b"%PDF-"):
        raise HTTPException(400, "File is not a PDF")
    try:
        PdfReader(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "PDF could not be read") from e
    sha = hashlib.sha256(data).hexdigest()
    if db.scalar(select(Resume).where(Resume.sha256 == sha)):
        raise HTTPException(409, "This exact file is already uploaded")

    version = (db.scalar(select(func.max(Resume.version))) or 0) + 1
    safe_name = os.path.basename(file.filename or "resume.pdf")[:200]
    r = Resume(version=version, filename=safe_name, sha256=sha, size_bytes=len(data))
    db.execute(update(Resume).values(is_active=False))
    r.is_active = True
    db.add(r)
    db.flush()

    get_settings().resume_dir.mkdir(parents=True, exist_ok=True)
    path = _path(r)
    with open(path, "xb") as fh:  # "x": fail rather than overwrite
        fh.write(data)
    os.chmod(path, 0o444)
    log_event(db, "owner", "resume.uploaded", version=version, sha256=sha)
    db.commit()
    return ResumeOut.model_validate(r, from_attributes=True)


@router.post("/{resume_id}/activate")
def activate(resume_id: int, db: Session = Depends(get_db)) -> ResumeOut:
    r = db.get(Resume, resume_id)
    if r is None:
        raise HTTPException(404, "Not found")
    db.execute(update(Resume).values(is_active=False))
    r.is_active = True
    log_event(db, "owner", "resume.activated", version=r.version)
    db.commit()
    return ResumeOut.model_validate(r, from_attributes=True)


@router.get("/{resume_id}/file")
def download(resume_id: int, db: Session = Depends(get_db)) -> FileResponse:
    r = db.get(Resume, resume_id)
    if r is None:
        raise HTTPException(404, "Not found")
    return FileResponse(_path(r), media_type="application/pdf", filename=r.filename)


@router.get("/{resume_id}/text")
def extract_text(resume_id: int, db: Session = Depends(get_db)) -> dict:
    """Plain text from the PDF, used to pre-fill the profile (owner confirms each fact)."""
    r = db.get(Resume, resume_id)
    if r is None:
        raise HTTPException(404, "Not found")
    reader = PdfReader(_path(r))
    return {"pages": [p.extract_text() or "" for p in reader.pages]}
