from tests.conftest import PASSWORD, H


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_api_requires_login(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/profile/facts").status_code == 401


def test_setup_once_then_login(client):
    st = client.get("/api/auth/state").json()
    assert (st["setup_done"], st["logged_in"], st["via_tailscale"]) == (False, False, False)
    assert client.post("/api/auth/setup", json={"password": PASSWORD}, headers=H).status_code == 200
    assert client.get("/api/auth/state").json()["logged_in"] is True
    # Setup can't be run again (nobody can hijack the owner account).
    assert client.post("/api/auth/setup", json={"password": "another long pw"}, headers=H).status_code == 409
    client.post("/api/auth/logout", headers=H)
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"password": "wrong password!"}, headers=H).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}, headers=H).status_code == 200
    assert client.get("/api/jobs").status_code == 200


def test_short_password_rejected(client):
    assert client.post("/api/auth/setup", json={"password": "short"}, headers=H).status_code == 400


def test_csrf_header_required_for_writes(owner):
    r = owner.post("/api/jobs", json={"url": "https://example.com/job/1"})
    assert r.status_code == 403


def test_login_throttle(client):
    client.post("/api/auth/setup", json={"password": PASSWORD}, headers=H)
    client.cookies.clear()
    for _ in range(5):
        client.post("/api/auth/login", json={"password": "nope nope nope"}, headers=H)
    r = client.post("/api/auth/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 429


def test_password_change_invalidates_old_sessions(owner):
    old_cookie = owner.cookies.get("jp_session")
    r = owner.post(
        "/api/auth/change-password",
        json={"current_password": PASSWORD, "new_password": "brand new password 2"},
        headers=H,
    )
    assert r.status_code == 200
    owner.cookies.clear()
    owner.cookies.set("jp_session", old_cookie)
    assert owner.get("/api/jobs").status_code == 401


def test_no_api_docs_exposed(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


# --- Passwordless login via Tailscale identity ---------------------------------

TS_OWNER = "owner@example.com"


def _ts_on(monkeypatch):
    monkeypatch.setenv("JOBPILOT_TAILSCALE_OWNER_LOGIN", TS_OWNER)
    from app.config import get_settings

    get_settings.cache_clear()


def test_tailscale_owner_header_logs_in_without_password(client, monkeypatch):
    _ts_on(monkeypatch)
    ts = {"Tailscale-User-Login": "Owner@Example.com"}
    st = client.get("/api/auth/state", headers=ts).json()
    assert st["logged_in"] and st["via_tailscale"]
    assert client.get("/api/jobs", headers=ts).status_code == 200


def test_tailscale_still_requires_csrf_header_on_writes(client, monkeypatch):
    _ts_on(monkeypatch)
    ts = {"Tailscale-User-Login": TS_OWNER}
    assert client.post("/api/jobs", json={"url": "https://boards.greenhouse.io/x/jobs/1"}, headers=ts).status_code == 403
    r = client.post("/api/jobs", json={"url": "https://boards.greenhouse.io/x/jobs/1"}, headers={**ts, **H})
    assert r.status_code in (200, 201)


def test_tailscale_wrong_login_is_rejected(client, monkeypatch):
    _ts_on(monkeypatch)
    r = client.get("/api/jobs", headers={"Tailscale-User-Login": "someone-else@example.com"})
    assert r.status_code == 401


def test_tailscale_header_ignored_when_feature_off(client):
    r = client.get("/api/jobs", headers={"Tailscale-User-Login": TS_OWNER})
    assert r.status_code == 401
    assert client.get("/api/auth/state", headers={"Tailscale-User-Login": TS_OWNER}).json()["via_tailscale"] is False
