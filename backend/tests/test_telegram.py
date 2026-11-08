from datetime import datetime

import pytest
from sqlalchemy import select

from app import db as dbmod
from app.models import Event, Fact, OwnerQuestion
from app.questions import create_question
from app.settings_store import AutomationSettings, get_setting
from app.telegram.bot import Bot

OWNER = 111
STRANGER = 999


class FakeAPI:
    def __init__(self):
        self.sent: list[dict] = []
        self.calls: list[tuple] = []
        self._mid = 100

    async def send(self, chat_id, text, buttons=None):
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons,
                          "message_id": self._mid})
        return {"message_id": self._mid}

    async def call(self, method, **params):
        self.calls.append((method, params))
        return True


def msg(text, sender=OWNER, reply_to=None):
    m = {"from": {"id": sender}, "chat": {"id": sender}, "text": text}
    if reply_to:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"message": m}


@pytest.fixture
def api():
    return FakeAPI()


@pytest.fixture
def bot(api):
    return Bot(api, OWNER)


async def test_stranger_is_ignored_and_logged(bot, api, db):
    await bot.handle_update(msg("/pause", sender=STRANGER))
    assert api.sent == []
    assert get_setting(db, "automation", AutomationSettings).paused is True  # default unchanged
    ev = db.scalar(select(Event).where(Event.type == "telegram.ignored_sender"))
    assert ev.data == {"sender_id": STRANGER}


async def test_setup_mode_reports_user_id(api):
    b = Bot(api, None)
    await b.handle_update(msg("hi", sender=4242))
    assert "4242" in api.sent[0]["text"]


async def test_pause_unpause(bot, api, db):
    await bot.handle_update(msg("/unpause"))
    db.expire_all()
    assert get_setting(db, "automation", AutomationSettings).paused is False
    await bot.handle_update(msg("/stop"))
    db.expire_all()
    assert get_setting(db, "automation", AutomationSettings).paused is True


async def test_status(bot, api):
    await bot.handle_update(msg("/status"))
    assert "JobPilot status" in api.sent[0]["text"]
    assert "No resume uploaded yet." in api.sent[0]["text"]


async def test_question_button_answer_saves_to_profile(bot, api, db):
    q = create_question(db, "Willing to work weekends?", ["Yes", "No"])
    db.commit()
    await bot.push_open_questions()
    sent = api.sent[-1]
    assert sent["buttons"][1][0]["callback_data"] == f"q:{q.id}:1"
    await bot.handle_update(
        {"callback_query": {"id": "cb1", "from": {"id": OWNER}, "data": f"q:{q.id}:1"}}
    )
    db.expire_all()
    fact = db.scalar(select(Fact).where(Fact.section == "answers"))
    assert fact.value == "No" and fact.source == "telegram" and fact.status == "confirmed"
    assert db.get(OwnerQuestion, q.id).status == "answered"
    # Not pushed twice.
    n = len(api.sent)
    await bot.push_open_questions()
    assert len(api.sent) == n


async def test_reply_text_answers_specific_question(bot, api, db):
    q1 = create_question(db, "Earliest start date?")
    q2 = create_question(db, "Desired salary?")
    db.commit()
    await bot.push_open_questions()
    q1_msg = next(s for s in api.sent if "Earliest" in s["text"])["message_id"]
    await bot.handle_update(msg("Two weeks after offer", reply_to=q1_msg))
    db.expire_all()
    assert db.get(OwnerQuestion, q1.id).answer == "Two weeks after offer"
    assert db.get(OwnerQuestion, q2.id).status == "open"


async def test_ssn_answer_refused(bot, api, db):
    create_question(db, "Tax identifier?")
    db.commit()
    await bot.handle_update(msg("123-45-6789"))
    assert "Not saved" in api.sent[-1]["text"]
    db.expire_all()
    assert db.scalar(select(Fact)) is None


async def test_stranger_cannot_press_buttons(bot, api, db):
    q = create_question(db, "Relocate?", ["Yes", "No"])
    db.commit()
    await bot.handle_update(
        {"callback_query": {"id": "x", "from": {"id": STRANGER}, "data": f"q:{q.id}:0"}}
    )
    db.expire_all()
    assert db.get(OwnerQuestion, q.id).status == "open"


async def test_daily_summary_once_per_day(bot, api, db):
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Los_Angeles")
    at = datetime(2026, 11, 6, 20, 5, tzinfo=tz)
    assert await bot.maybe_send_daily_summary(at) is True
    assert await bot.maybe_send_daily_summary(at) is False
    assert await bot.maybe_send_daily_summary(at.replace(hour=9)) is False
    assert sum("Daily summary" in s["text"] for s in api.sent) == 1


def test_db_fixture_isolated():
    s = dbmod.session_factory()()
    assert s.scalar(select(Fact)) is None
    s.close()


def test_http_client_logs_do_not_leak_token():
    """httpx logs full URLs (which contain the bot token) at INFO; they must be silenced."""
    import logging

    import app.main  # noqa: F401  (configures logging on import)

    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_decision_questions_do_not_become_profile_facts(db):
    from app.models import Fact
    from app.questions import answer_question, create_question, latest_open

    q = create_question(db, "Ready to submit?", ["Submit", "Cancel"], kind="approval")
    db.commit()
    # Free-text replies must never be taken as a Submit decision.
    assert latest_open(db) is None
    import pytest

    with pytest.raises(ValueError):
        answer_question(db, q, "sure why not", actor="telegram")
    answer_question(db, q, "Submit", actor="telegram")
    db.commit()
    assert q.status == "answered" and q.answer == "Submit"
    assert db.query(Fact).count() == 0


async def test_info_reply_is_split_into_facts(bot, api, db, monkeypatch):
    """One casual reply to several questions becomes several standard facts."""
    import asyncio

    from app.ai import client as ai_client
    from app.ai.client import AIResult

    seen = {}

    async def fake_run_task(task, system, prompt, schema, **kw):
        seen["prompt"] = prompt
        return AIResult({"facts": [
            {"section": "contact", "label": "Street address", "value": "1 Main St"},
            {"section": "contact", "label": "ZIP code", "value": "97301"},
            {"section": "contact", "label": "SSN", "value": "123-45-6789"},
        ], "unanswered": ["What is your desired salary?"]}, "codex", "", "", 1)

    monkeypatch.setattr("app.info_answers.run_task", fake_run_task)
    assert ai_client  # (module imported so the patch target exists)
    q = create_question(db, "1. Street address?\n2. ZIP code?\n3. Salary?", [], kind="info")
    db.commit()
    await bot.handle_update(msg("1 Main St, Salem OR 97301"))
    await asyncio.gather(*bot._tasks)
    db.expire_all()
    facts = {f.label: f.value for f in db.scalars(select(Fact)).all()}
    assert facts["Street address"] == "1 Main St" and facts["ZIP code"] == "97301"
    assert "SSN" not in facts
    assert db.get(OwnerQuestion, q.id).status == "answered"
    last = api.sent[-1]["text"]
    assert "ZIP code: 97301" in last and "Still need" in last and "Not saved" in last
    assert "1 Main St, Salem OR 97301" in seen["prompt"]


async def test_setup_asks_first_group_in_one_message(bot, api, db):
    await bot.handle_update(msg("/setup"))
    q = db.scalar(select(OwnerQuestion).where(OwnerQuestion.section == "setup"))
    assert q.kind == "info" and q.status == "open"
    assert "Street address" in api.sent[-1]["text"] and "ZIP code" in api.sent[-1]["text"]
    await bot.handle_update(msg("/setup"))  # asking again replaces the old one
    db.expire_all()
    assert db.get(OwnerQuestion, q.id).status == "expired"


def test_common_questions_api(owner):
    client = owner
    h = {"X-JobPilot": "1"}
    r = client.get("/api/profile/common", headers=h)
    assert r.status_code == 200
    rows = {x["id"]: x for x in r.json()}
    assert rows["zip"]["label"] == "ZIP code" and rows["zip"]["value"] is None
    r = client.post("/api/profile/common", json={"answers": {"zip": "97301", "authorized": "Yes"}}, headers=h)
    assert r.json() == {"saved": 2}
    bad = client.post("/api/profile/common", json={"answers": {"authorized": "Maybe"}}, headers=h)
    assert bad.status_code == 400
    rows = {x["id"]: x for x in client.get("/api/profile/common", headers=h).json()}
    assert rows["zip"]["value"] == "97301"
