"""Start/stop an application run, see its status, and view its screenshots."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from app.apply import batch, worker
from app.auth import require_owner
from app.config import get_settings

router = APIRouter(prefix="/api", tags=["apply"], dependencies=[Depends(require_owner)])


@router.post("/jobs/{job_id}/apply")
async def start_apply(job_id: int, force: bool = False) -> dict:
    """force=true skips the fit check ("Apply anyway")."""
    try:
        await worker.start(job_id, force=force)
    except worker.ApplyError as e:
        raise HTTPException(409, str(e)) from e
    return worker.status()


@router.get("/apply/status")
def apply_status() -> dict:
    return {**worker.status(), "batch": batch.status()}


@router.post("/apply/stop")
def apply_stop() -> dict:
    """Stops the application in progress and any batch."""
    batch.stop()
    return apply_status()


@router.post("/apply/batch")
async def start_batch(n: int | None = Query(default=None, ge=1, le=50)) -> dict:
    """Apply to the next n jobs (default: the batch size in Settings)."""
    try:
        await batch.start(n)
    except worker.ApplyError as e:
        raise HTTPException(409, str(e)) from e
    return apply_status()


@router.get("/shots/{path:path}")
def screenshot(path: str) -> FileResponse:
    """Serve a run screenshot. Only PNGs under data/runs/ (no path tricks)."""
    runs = get_settings().runs_dir.resolve()
    full = (get_settings().data_dir / path).resolve()
    if not full.is_relative_to(runs) or full.suffix != ".png" or not full.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(Path(full), media_type="image/png")
