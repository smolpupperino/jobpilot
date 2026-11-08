from app.events import _redact
from tests.conftest import H


def _add(c, **kw):
    body = {"section": "contact", "label": "First name", "value": "Alex"} | kw
    return c.post("/api/profile/facts", json=body, headers=H)


def test_create_edit_history(owner):
    f = _add(owner).json()
    assert f["status"] == "confirmed" and f["source"] == "dashboard" and f["confirmed_at"]
    r = owner.patch(f"/api/profile/facts/{f['id']}", json={"value": "Alexander"}, headers=H)
    assert r.json()["value"] == "Alexander"
    hist = owner.get(f"/api/profile/facts/{f['id']}/history").json()
    assert [h["value"] for h in hist] == ["Alex", "Alexander"]


def test_same_label_upserts_not_duplicates(owner):
    _add(owner, section="answers", label="Willing to relocate?", value="No")
    _add(owner, section="answers", label="willing to relocate", value="Yes")
    facts = owner.get("/api/profile/facts").json()
    assert len(facts) == 1 and facts[0]["value"] == "Yes"


def test_unconfirmed_has_no_confirmed_at(owner):
    f = _add(owner, status="unconfirmed").json()
    assert f["confirmed_at"] is None


def test_ssn_and_passwords_refused(owner):
    assert _add(owner, label="Social Security Number", value="x").status_code == 400
    assert _add(owner, label="SSN", value="x").status_code == 400
    assert _add(owner, label="Workday password", value="x").status_code == 400
    assert _add(owner, label="Tax ID", value="123-45-6789").status_code == 400
    assert owner.get("/api/profile/facts").json() == []


def test_bad_section_rejected(owner):
    assert _add(owner, section="secrets").status_code == 400


def test_delete(owner):
    f = _add(owner).json()
    assert owner.delete(f"/api/profile/facts/{f['id']}", headers=H).status_code == 200
    assert owner.get("/api/profile/facts").json() == []


def test_event_redaction():
    out = _redact({"password": "p", "nested": {"api_token": "t"}, "ok": 1})
    assert out == {"password": "[redacted]", "nested": {"api_token": "[redacted]"}, "ok": 1}
