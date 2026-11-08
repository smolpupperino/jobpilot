"""SQLite engine and session handling (WAL mode, foreign keys on)."""

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _set_sqlite_pragmas(dbapi_conn, _record) -> None:  # noqa: ANN001
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def make_engine(url: str) -> Engine:
    eng = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(eng, "connect", _set_sqlite_pragmas)
    return eng


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    if url is None:
        settings = get_settings()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{settings.db_path}"
    _engine = make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()
