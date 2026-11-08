from datetime import UTC, datetime, timedelta

import pytest

from app.apply.page import PageState
from app.apply.planner import check_plan
from app.mail import Message, _parse, extract_code, extract_link, related
from app.models import SiteAccount
from app.vault import decrypt, encrypt, generate_password, get_or_create_account, password_of, site_key


def test_password_rules():
    for _ in range(50):
        pw = generate_password()
        assert len(pw) == 20 and pw[0].isalnum()
        assert any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_encrypt_is_bound_to_site():
    tok = encrypt("s3cret", "a.example.com")
    assert decrypt(tok, "a.example.com") == "s3cret"
    with pytest.raises(Exception):  # noqa: B017  (AES-GCM auth failure)
        decrypt(tok, "b.example.com")


def test_one_shared_password_across_sites(db):
    a = get_or_create_account(db, "https://acme.wd5.myworkdayjobs.com/en-US/careers", "me@gmail.com")
    b = get_or_create_account(db, "careers-foo.icims.com", "me@gmail.com")
    db.commit()
    assert a.site == "acme.wd5.myworkdayjobs.com" and b.site == "careers-foo.icims.com"
    assert password_of(a) == password_of(b)
    assert a.password_enc != b.password_enc  # encrypted separately
    assert db.query(SiteAccount).count() == 2
    # The DB never holds the plaintext.
    assert password_of(a) not in a.password_enc


def test_site_key():
    assert site_key("https://www.Acme.com/jobs?x=1") == "acme.com"


def test_workday_family_matches_sender_and_links():
    assert related("acme.wd5.myworkdayjobs.com", "myworkday.com")
    assert related("acme.wd5.myworkdayjobs.com", "other.wd1.myworkdayjobs.com")
    assert not related("acme.wd5.myworkdayjobs.com", "evil-myworkdayjobs.com.attacker.net")
    assert not related("acme.wd5.myworkdayjobs.com", "gmail.com")


def _msg(subject, html, sender="noreply@myworkday.com"):
    raw = (f"From: Acme <{sender}>\r\nSubject: {subject}\r\nDate: "
           f"{datetime.now(UTC).strftime('%a, %d %b %Y %H:%M:%S +0000')}\r\n"
           "Content-Type: text/html; charset=utf-8\r\n\r\n" + html).encode()
    return _parse(raw, datetime.now(UTC))


def test_extract_code():
    m = _msg("Verify your account", "<p>Your verification code is <b>482913</b>. It expires in 2026.</p>")
    assert extract_code(m) == "482913"


def test_extract_link_only_from_site_family():
    m = _msg("Verify your email", '<a href="https://evil.example/verify?t=1">x</a>'
             '<a href="https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/abc123">Verify</a>')
    assert extract_link(m, "acme.wd5.myworkdayjobs.com").endswith("/activate/abc123")
    m2 = _msg("Verify", '<a href="https://evil.example/verify?t=1">x</a>')
    assert extract_link(m2, "acme.wd5.myworkdayjobs.com") is None


def _st(fields):
    return PageState(frames=[{"url": "u", "title": "t", "headings": [], "errors": [], "text": "",
                              "fields": fields, "buttons": []}])


EMAIL = {"id": "e1", "kind": "email", "label": "Email Address", "required": True, "value": ""}
PW = {"id": "e2", "kind": "password", "label": "Password", "required": True, "value": ""}
CODE = {"id": "e3", "kind": "text", "label": "Verification code", "required": True, "value": ""}


def _p(kind, fills):
    return {"page_kind": kind, "fills": fills, "clicks": [], "questions": [], "advance_button_id": "",
            "advance_kind": "none", "blocker": "", "notes": ""}


def test_account_actions_allowed_only_on_account_pages():
    fills = [{"field_id": "0:e1", "action": "account_email", "value": "", "fact_ids": []},
             {"field_id": "0:e2", "action": "account_password", "value": "", "fact_ids": []}]
    ok = check_plan(_p("create_account", fills), _st([EMAIL, PW]), set())
    assert [a.action for a in ok.actions] == ["account_email", "account_password"]
    bad = check_plan(_p("application_form", fills), _st([EMAIL, PW]), set())
    assert bad.actions == []


def test_password_never_taken_from_ai_value():
    fills = [{"field_id": "0:e2", "action": "account_password", "value": "leaked!", "fact_ids": []}]
    cp = check_plan(_p("sign_in", fills), _st([PW]), set())
    assert cp.actions[0].value == ""  # the worker fills from the vault, not from the AI


def test_verification_code_only_on_code_page():
    fills = [{"field_id": "0:e3", "action": "verification_code", "value": "", "fact_ids": []}]
    assert check_plan(_p("verify_email_code", fills), _st([CODE]), set()).actions
    assert not check_plan(_p("application_form", fills), _st([CODE]), set()).actions


def test_old_message_type_still_constructible():
    m = Message(datetime.now(UTC) - timedelta(minutes=1), "a@b.c", "s", "t", [])
    assert m.sender == "a@b.c"


def test_honeypot_field_is_never_filled_or_asked():
    trap = {"id": "e9", "kind": "text", "required": True, "value": "",
            "label": "Enter website. This input is for robots only, do not enter if you're human."}
    fills = [{"field_id": "0:e9", "action": "fill", "value": "x", "fact_ids": [1]}]
    cp = check_plan(_p("application_form", fills), _st([trap]), {1})
    assert cp.actions == [] and cp.questions == []
    assert _st([trap]).for_prompt()["fields"] == []


def test_submit_button_on_sign_in_form_is_not_application_submit():
    st = PageState(frames=[{"url": "u", "title": "t", "headings": [], "errors": [], "text": "",
                            "fields": [EMAIL, PW], "buttons": [{"id": "b1", "text": "Submit", "type": "submit"}]}])
    plan = _p("sign_in", [{"field_id": "0:e1", "action": "account_email", "value": "", "fact_ids": []}])
    plan.update(advance_button_id="0:b1", advance_kind="submit")
    assert check_plan(plan, st, set()).advance_kind == "next"
    plan["page_kind"] = "application_form"
    assert check_plan(plan, st, set()).advance_kind == "submit"
