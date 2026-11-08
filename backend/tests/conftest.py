import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JOBPILOT_SESSION_SECRET", "test-secret-" + "x" * 40)
os.environ["JOBPILOT_COOKIE_SECURE"] = "false"
os.environ["JOBPILOT_VAULT_KEY"] = "q83vEi8nLc0sX5U8yK5b9Yb1kU2tF9mJ3wQ7Zp1aR6c="
os.environ.pop("JOBPILOT_TELEGRAM_BOT_TOKEN", None)

from app import auth  # noqa: E402
from app import db as dbmod
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402

PASSWORD = "correct horse battery"
H = {"X-JobPilot": "1"}


@pytest.fixture(autouse=True)
def _fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBPILOT_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    eng = dbmod.init_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(eng)
    auth._failures.clear()
    from app import job_details

    job_details.ENABLED = False  # no real network lookups from tests
    yield
    eng.dispose()
    get_settings.cache_clear()


@pytest.fixture
def client():
    from app.main import create_app

    with TestClient(create_app(start_background=False)) as c:
        yield c


@pytest.fixture
def owner(client):
    r = client.post("/api/auth/setup", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def db():
    s = dbmod.session_factory()()
    yield s
    s.close()
