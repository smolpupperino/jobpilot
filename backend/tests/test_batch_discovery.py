"""Batch runs (apply to the next N jobs) and job discovery from emails."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app import discovery
from app.apply import batch, worker
from app.events import log_event
from app.mail import Message, _parse
from app.models import Event, Job
from app.settings_store import AutomationSettings, get_setting, put_setting
from app.telegram import bot as botmod
from tests.conftest import H
from tests.test_telegram import OWNER, FakeAPI, msg


def _job(db, n, status="new", ats="greenhouse", employer="Acme", title="Analyst", url=None, created=None):
    url = url or f"https://boards.greenhouse.io/acme/jobs/{n}"
    j = Job(url=url, canonical_url=url, employer=employer, title=title, ats=ats, status=status)
    if created:
        j.created_at = created
    db.add(j)
    db.flush()
    return j


def _unpause(db, **kw):
    a = get_setting(db, "automation", AutomationSettings)
    a.paused = False
    for k, v in kw.items():
        setattr(a, k, v)
    put_setting(db, "automation", a)
    db.commit()


def _drain() -> list[str]:
    out = []
    while not botmod._notify_queue.empty():
        out.append(botmod._notify_queue.get_nowait()[0])
    return out


@pytest.fixture(autouse=True)
def _clean():
    _drain()
    batch._state.update(running=False)
    yield
    _drain()


# --- batch --------------------------------------------------------------------

def test_next_job_order_and_exclusions(db):
    now = datetime.now(UTC).replace(tzinfo=None)
    old_new = _job(db, 1, "new", created=now - timedelta(days=2))
    new_new = _job(db, 2, "new", created=now - timedelta(days=1))
    interested = _job(db, 3, "interested", created=now - timedelta(days=5))
    _job(db, 4, "new", ats="linkedin", url="https://www.linkedin.com/jobs/view/123456789/")
    failed = _job(db, 5, "interested", created=now)
    _job(db, 6, "submitted_confirmed")
    log_event(db, "system", "apply.finished", job_id=failed.id, outcome="error")
    db.commit()
    assert batch.next_job(set()) == interested.id  # Interested first, even if older
    assert batch.next_job({interested.id}) == new_new.id  # then New, newest first
    assert batch.next_job({interested.id, new_new.id}) == old_new.id
    assert batch.next_job({interested.id, new_new.id, old_new.id}) is None  # no LinkedIn, no failed


async def test_batch_counts_only_submitted_and_summarizes(db, monkeypatch):
    jobs = [_job(db, i, "new", title=f"Job{i}",
                 created=datetime(2026, 1, 10 - i)) for i in range(1, 6)]  # Job1 newest
    _unpause(db, batch_size=2)
    outcomes = {jobs[0].id: ("skipped_fit", False), jobs[1].id: ("confirmed", True),
                jobs[2].id: ("error", False), jobs[3].id: ("confirmed", True)}
    started: list[int] = []

    async def fake_start(job_id, force=False):
        started.append(job_id)
        o, sub = outcomes[job_id]
        worker._last.clear()
        worker._last.update(job_id=job_id, outcome=o, submitted=sub)
        with batch._db() as s:
            s.get(Job, job_id).status = "skipped" if o == "skipped_fit" else "submitted_confirmed"
            if o == "skipped_fit":
                log_event(s, "system", "apply.fit", job_id=job_id, score=30)
            s.commit()

    async def fake_wait():
        return None

    monkeypatch.setattr(worker, "start", fake_start)
    monkeypatch.setattr(worker, "wait", fake_wait)
    await batch.start()
    await batch._task
    assert started == [j.id for j in jobs[:4]]  # stopped once 2 were submitted; Job5 untouched
    st = batch.status()
    assert st["running"] is False and st["submitted"] == 2 and st["tried"] == 4
    text = _drain()[-1]
    assert text.startswith("Batch done: applied to 2 of 2.")
    assert "Job1 at Acme (fit 30)" in text and "Failed" in text and "Job3" in text
    assert db.scalar(select(Event).where(Event.type == "batch.finished")).data["submitted"] == 2


async def test_batch_stops_when_paused(db, monkeypatch):
    for i in range(3):
        _job(db, i, "new")
    _unpause(db, batch_size=3)
    calls = []

    async def fake_start(job_id, force=False):
        calls.append(job_id)
        worker._last.update(job_id=job_id, outcome="stopped", submitted=False)
        with batch._db() as s:
            a = get_setting(s, "automation", AutomationSettings)
            a.paused = True
            put_setting(s, "automation", a)
            s.get(Job, job_id).status = "interested"
            s.commit()

    async def fake_wait():
        return None

    monkeypatch.setattr(worker, "start", fake_start)
    monkeypatch.setattr(worker, "wait", fake_wait)
    await batch.start()
    await batch._task
    assert len(calls) == 1
    assert "automation was paused" in _drain()[-1]


async def test_batch_refuses_when_paused_or_empty(db):
    with pytest.raises(worker.ApplyError, match="paused"):
        await batch.start()
    _unpause(db)
    with pytest.raises(worker.ApplyError, match="No jobs"):
        await batch.start()


async def test_telegram_batch_commands(db, monkeypatch):
    api = FakeAPI()
    b = botmod.Bot(api, OWNER)
    started = []

    async def fake_start(n=None):
        started.append(n)
        return {}

    monkeypatch.setattr(batch, "start", fake_start)
    await b.handle_update(msg("/batchsize 7"))
    db.expire_all()
    assert get_setting(db, "automation", AutomationSettings).batch_size == 7
    assert started == [] and "set to 7" in api.sent[-1]["text"]
    await b.handle_update(msg("/batch 3"))
    db.expire_all()
    assert get_setting(db, "automation", AutomationSettings).batch_size == 3 and started == [3]
    await b.handle_update(msg("/batch"))
    assert started == [3, None]
    await b.handle_update(msg("/batch lots"))
    assert "1 to 50" in api.sent[-1]["text"] and len(started) == 2


def test_batch_api_and_settings(owner):
    r = owner.get("/api/apply/status")
    assert r.json()["batch"]["running"] is False
    a = owner.get("/api/settings/automation").json()
    assert a["batch_size"] == 5 and a["discovery"] is True
    assert owner.put("/api/settings/automation", json={**a, "batch_size": 0}, headers=H).status_code == 422
    assert owner.put("/api/settings/automation", json={**a, "batch_size": 12}, headers=H).status_code == 200
    r = owner.post("/api/apply/batch?n=3", headers=H)
    assert r.status_code == 409 and "paused" in r.json()["detail"]


# --- discovery: job alerts ----------------------------------------------------

LINKEDIN_ALERT = b"""From: LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>
Subject: "security analyst": 3 new jobs
Message-ID: <li-1@linkedin.com>
Date: Sat, 07 Nov 2026 10:00:00 +0000
Content-Type: text/html; charset=utf-8

<html><body>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc&refId=x">
  <img src="logo.png"></a>
<a href="https://www.linkedin.com/comm/jobs/view/4012345678/?trackingId=abc">SOC Analyst I</a>
<p>Acme Health &middot; Portland, OR (Hybrid)</p>
<a href="https://www.linkedin.com/comm/jobs/view/4099999999/">Security Analyst</a>
<p>Globex &middot; Remote</p>
<a href="https://www.linkedin.com/comm/jobs/search?keywords=security">See all jobs</a>
<a href="https://www.linkedin.com/comm/psettings/email-unsubscribe?x=1">Unsubscribe</a>
</body></html>
"""

COMPANY_ALERT = b"""From: Careers <no-reply@careers.example.com>
Subject: New jobs that match your job alert
Message-ID: <c-1@example.com>
Date: Sat, 07 Nov 2026 11:00:00 +0000
Content-Type: text/html; charset=utf-8

<a href="https://boards.greenhouse.io/initech/jobs/5551234?gh_src=alert&utm_source=x">IT Security Engineer</a>
<a href="https://initech.wd5.myworkdayjobs.com/en-US/Careers/job/Austin-TX/Help-Desk-Tech_R12345">View job</a>
<a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&ao=1&jobListingId=999">Glassdoor job</a>
<a href="https://click.example.com/redirect?u=https%3A%2F%2Fjobs.lever.co%2Fx%2F1">Tracked link</a>
<a href="https://boards.greenhouse.io/initech/jobs/5551234">Apply now</a>
<a href="https://initech.wd5.myworkdayjobs.com/en-US/Careers/userHome/settings">Manage alerts</a>
<a href="https://www.indeed.com/rc/clk/dl?jk=0123abcd4567ef89&from=ja">Network Technician</a>
"""


def test_linkedin_alert_parsed():
    m = _parse(LINKEDIN_ALERT, datetime.now(UTC))
    assert discovery.is_alert(m)
    leads = {lead.url: lead for lead in discovery.leads_from_alert(m)}
    assert set(leads) == {"https://www.linkedin.com/jobs/view/4012345678/",
                          "https://www.linkedin.com/jobs/view/4099999999/"}
    first = leads["https://www.linkedin.com/jobs/view/4012345678/"]
    assert first.title == "SOC Analyst I" and first.employer == "Acme Health"
    assert first.source == "linkedin_alert" and first.ats == "linkedin"


def test_company_alert_keeps_only_direct_posting_links():
    m = _parse(COMPANY_ALERT, datetime.now(UTC))
    assert discovery.is_alert(m)
    leads = {lead.ats: lead for lead in discovery.leads_from_alert(m)}
    assert set(leads) == {"greenhouse", "workday", "indeed"}  # no Glassdoor/redirects/settings
    gh = leads["greenhouse"]
    assert gh.url == "https://boards.greenhouse.io/initech/jobs/5551234"  # tracking stripped
    assert gh.title == "IT Security Engineer" and gh.employer == "Initech"
    assert leads["workday"].employer == "Initech" and leads["workday"].title == ""
    assert leads["indeed"].url == "https://www.indeed.com/viewjob?jk=0123abcd4567ef89"


def test_status_emails_are_not_alerts():
    m = Message(datetime.now(UTC), "no-reply@greenhouse.io", "Thank you for applying to Initech", "", [])
    assert not discovery.is_alert(m)
    m = Message(datetime.now(UTC), "messages-noreply@linkedin.com", "Bob viewed your profile", "", [])
    assert not discovery.is_alert(m)


def test_process_adds_dedupes_and_remembers_seen(db):
    _job(db, 0, url="https://boards.greenhouse.io/initech/jobs/5551234")  # already tracked
    db.commit()
    msgs = [_parse(LINKEDIN_ALERT, datetime.now(UTC)), _parse(COMPANY_ALERT, datetime.now(UTC))]
    r = discovery.process(db, msgs)
    db.commit()
    assert r["alerts"] == 2 and len(r["added"]) == 4  # 2 LinkedIn + Workday + Indeed
    assert db.query(Job).count() == 5
    assert all(j.status == "new" for j in db.scalars(select(Job).where(Job.source != "manual")))
    text = discovery.report_text(r)
    assert "Found 4 new jobs" in text and "2 are LinkedIn links" in text and "/batch" in text
    again = discovery.process(db, msgs)  # same emails next scan: nothing new
    assert again == {"alerts": 0, "added": [], "replies": []}


# --- discovery: employer replies ----------------------------------------------

def _reply(sender, subject, body):
    return Message(datetime.now(UTC), sender, subject, body, [], msg_id=f"{sender}{subject}")


def test_rejection_moves_status_and_lands_on_timeline(db):
    j = _job(db, 1, "submitted_confirmed", employer="Initech, Inc.",
             url="https://boards.greenhouse.io/initech/jobs/1")
    _job(db, 2, "submitted_confirmed", employer="Globex", url="https://boards.greenhouse.io/globex/jobs/2")
    db.commit()
    m = _reply("no-reply@greenhouse.io", "Your application to Initech",
               "Thank you for applying. Unfortunately we have decided to move forward with other candidates.")
    r = discovery.process(db, [m])
    db.commit()
    assert r["replies"][0]["kind"] == "rejected" and r["replies"][0]["job_id"] == j.id
    db.refresh(j)
    assert j.status == "rejected"
    ev = db.scalar(select(Event).where(Event.type == "email.received"))
    assert ev.job_id == j.id and ev.data["subject"] == "Your application to Initech"


def test_ambiguous_reply_is_not_linked(db):
    _job(db, 1, "submitted_confirmed", employer="", url="https://boards.greenhouse.io/a/jobs/1")
    _job(db, 2, "submitted_confirmed", employer="", url="https://boards.greenhouse.io/b/jobs/2")
    db.commit()
    m = _reply("no-reply@greenhouse.io", "Application update", "We'd like to schedule an interview.")
    assert discovery.process(db, [m])["replies"] == []


def test_interview_and_received_classification(db):
    j = _job(db, 1, "submitted_uncertain", employer="Globex")
    db.commit()
    ack = _reply("talent@globex.com", "Globex: application received",
                 "We have received your application. If selected we will contact you for an interview.")
    r = discovery.process(db, [ack])
    db.commit()
    db.refresh(j)
    assert r["replies"][0]["kind"] == "acknowledged" and j.status == "acknowledged"
    inv = _reply("talent@globex.com", "Next steps with Globex",
                 "We would like to invite you to a phone interview. Please share your availability.")
    r = discovery.process(db, [inv])
    db.commit()
    db.refresh(j)
    assert j.status == "interview"
    assert discovery.report_text(r).startswith("Interview request: Analyst at Globex")
    # A late "received" email never moves an interview back.
    late = _reply("talent@globex.com", "Thanks for applying to Globex", "Thank you for applying!")
    discovery.process(db, [late])
    db.commit()
    db.refresh(j)
    assert j.status == "interview"


def test_due_respects_settings():
    now = datetime.now(UTC)
    a = AutomationSettings(paused=False, discovery=True, discovery_interval_minutes=60)
    assert discovery._due(a, None, now)
    assert not discovery._due(a, (now - timedelta(minutes=10)).isoformat(), now)
    assert discovery._due(a, (now - timedelta(minutes=61)).isoformat(), now)
    assert not discovery._due(AutomationSettings(paused=True), None, now)
    assert not discovery._due(AutomationSettings(paused=False, discovery=False), None, now)


def test_ats_mail_domains_match_their_job_sites():
    from app.mail import related

    assert related("job-boards.greenhouse.io", "us.greenhouse-mail.io")
    assert related("firsttechfedcareers.ttcportals.com", "jobvite.com")
    assert not related("job-boards.greenhouse.io", "jobvite.com")
