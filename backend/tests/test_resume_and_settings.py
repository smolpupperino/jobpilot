import hashlib
import io
import os

from pypdf import PdfWriter

from tests.conftest import H


def _pdf() -> bytes:
    w = PdfWriter()
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_upload_is_hashed_readonly_and_versioned(owner, tmp_path):
    data = _pdf()
    r = owner.post("/api/resumes", files={"file": ("me.pdf", data, "application/pdf")}, headers=H)
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["sha256"] == hashlib.sha256(data).hexdigest()
    assert res["version"] == 1 and res["is_active"]
    path = tmp_path / "resumes" / f"v1-{res['sha256'][:12]}.pdf"
    assert path.read_bytes() == data
    assert not os.access(path, os.W_OK) or os.geteuid() == 0
    assert oct(path.stat().st_mode & 0o777) == "0o444"
    # Same file again is refused; downloading returns exact bytes.
    again = owner.post("/api/resumes", files={"file": ("me.pdf", data, "application/pdf")}, headers=H)
    assert again.status_code == 409
    assert owner.get(f"/api/resumes/{res['id']}/file").content == data


def test_non_pdf_rejected(owner):
    r = owner.post("/api/resumes", files={"file": ("x.pdf", b"hello", "application/pdf")}, headers=H)
    assert r.status_code == 400


def test_new_version_becomes_active(owner):
    w = PdfWriter()
    w.add_blank_page(612, 792)
    w.add_blank_page(612, 792)
    buf = io.BytesIO()
    w.write(buf)
    owner.post("/api/resumes", files={"file": ("a.pdf", _pdf(), "application/pdf")}, headers=H)
    owner.post("/api/resumes", files={"file": ("b.pdf", buf.getvalue(), "application/pdf")}, headers=H)
    rs = owner.get("/api/resumes").json()
    assert [(r["version"], r["is_active"]) for r in rs] == [(2, True), (1, False)]


def test_ai_routes_per_task(owner):
    cur = owner.get("/api/settings/ai").json()
    assert set(cur["tasks"]) == {"form_mapper", "job_fit", "email_classifier", "chat"}
    s = cur["settings"]
    s["tasks"]["form_mapper"] = {"provider": "codex", "model": "", "fallback_provider": "claude",
                                 "fallback_model": "haiku"}
    r = owner.put("/api/settings/ai", json=s, headers=H)
    assert r.status_code == 200
    assert owner.get("/api/settings/ai").json()["settings"]["tasks"]["form_mapper"]["provider"] == "codex"


def test_ai_bad_provider_rejected(owner):
    s = owner.get("/api/settings/ai").json()["settings"]
    s["tasks"]["chat"]["provider"] = "skynet"
    assert owner.put("/api/settings/ai", json=s, headers=H).status_code == 422


def test_automation_defaults_safe(owner):
    a = owner.get("/api/settings/automation").json()
    assert a["paused"] is True and a["dry_run"] is True


def test_questions_dashboard_flow(owner):
    q = owner.post("/api/questions", json={"question": "Willing to travel 25%?",
                                           "options": ["Yes", "No"]}, headers=H).json()
    r = owner.post(f"/api/questions/{q['id']}/answer", json={"answer": "Yes"}, headers=H)
    assert r.json()["status"] == "answered"
    facts = owner.get("/api/profile/facts").json()
    assert facts[0]["label"] == "Willing to travel 25%?" and facts[0]["value"] == "Yes"
    assert owner.post(f"/api/questions/{q['id']}/answer", json={"answer": "No"},
                      headers=H).status_code == 409


def test_model_choices_from_models_json(owner, tmp_path):
    import json

    (tmp_path / "models.json").write_text(json.dumps({"codex": ["gpt-x-luna", "gpt-x-sol"], "claude": ["haiku"]}))
    ms = owner.get("/api/settings/ai").json()["model_suggestions"]
    assert ms["codex"] == ["", "gpt-x-luna", "gpt-x-sol"]
    assert ms["claude"] == ["", "haiku"]


def test_model_choices_ignore_bad_models_json(owner, tmp_path):
    (tmp_path / "models.json").write_text("{not json")
    ms = owner.get("/api/settings/ai").json()["model_suggestions"]
    assert "sonnet" in ms["claude"] and ms["codex"] == [""]


def test_effort_levels_saved_and_validated(owner, tmp_path):
    import json

    (tmp_path / "models.json").write_text(json.dumps({"efforts": {"codex": {"gpt-x-luna": ["low", "high"]}}}))
    body = owner.get("/api/settings/ai").json()
    assert body["effort_choices"]["codex"]["gpt-x-luna"] == ["low", "high"]
    assert body["effort_choices"]["claude"]["*"][-1] == "max"

    tasks = body["settings"]["tasks"]
    tasks["form_mapper"].update(provider="codex", model="gpt-x-luna", effort="high")
    r = owner.put("/api/settings/ai", json={"tasks": tasks}, headers=H)
    assert r.status_code == 200, r.text
    assert owner.get("/api/settings/ai").json()["settings"]["tasks"]["form_mapper"]["effort"] == "high"

    tasks["form_mapper"]["effort"] = "turbo"
    assert owner.put("/api/settings/ai", json={"tasks": tasks}, headers=H).status_code == 422


def test_fit_check_settings_defaults_and_validation(owner):
    a = owner.get("/api/settings/automation").json()
    assert a["fit_check"] is True and a["min_fit_score"] == 50
    a["min_fit_score"] = 150
    assert owner.put("/api/settings/automation", json=a, headers=H).status_code == 422


def test_providers_and_default_model_efforts(owner):
    from tests.conftest import H

    d = owner.get("/api/settings/ai").json()
    assert d["providers"] == ["claude", "codex", "gemini", "none"]
    assert d["effort_choices"]["claude"]["*"]
    tasks = d["settings"]["tasks"]
    tasks["chat"]["fallback_provider"] = "gemini"
    assert owner.put("/api/settings/ai", json={"tasks": tasks}, headers=H).status_code == 200
    tasks["chat"]["provider"] = "openai"
    assert owner.put("/api/settings/ai", json={"tasks": tasks}, headers=H).status_code == 422
