"""Run the REAL application worker (worker._run_locked) against the local fixture site,
with the AI calls answered by deterministic stand-ins that read the same prompts.

Used by the end-to-end tests and the benchmark. Everything is local: a separate headless
Chromium reached over CDP (like JobPilot's browser container), a temp database, the
fixture HTTP server. Nothing touches the live browser, the real database or any real site.
"""

import asyncio
import contextlib
import hashlib
import json
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field

from tests.browser.policy import HeuristicDecider

FACTS = [
    ("basics", "First name", "Nathan"),
    ("basics", "Last name", "Byrne"),
    ("basics", "Email", "test.applicant@example.com"),
    ("basics", "State", "Oregon"),
    ("work_authorization", "Work authorization", "Yes"),
    ("application", "How did you hear about us", "Career Websites > Corporate Website"),
    ("education", "Highest level of education completed", "Bachelor's Degree"),
]
# What the fixture application must receive (hidden inputs the site posts).
EXPECTED_POST = {"first": "Nathan", "last": "Byrne", "email": "test.applicant@example.com", "state": "Oregon",
                 "auth": "Yes", "heard": "Corporate Website", "degree": "Bachelor's Degree", "agree": "on"}
# Page label -> fact label (what a planner model works out from wording).
LABEL_MAP = [
    (r"first name", "First name"), (r"last name", "Last name"), (r"e-?mail", "Email"), (r"\bstate\b", "State"),
    (r"authori[sz]ed to work", "Work authorization"), (r"hear about", "How did you hear about us"),
    (r"education|degree", "Highest level of education completed"),
]
DISMISS = re.compile(r"^(no thanks|not now|close|dismiss)$", re.I)


def fake_plan(prompt: str, schema: dict) -> dict:
    """A planner stand-in: maps visible fields to facts by wording, picks the forward button.
    Reads only the prompt (facts JSON + page JSON), like the model."""
    facts = json.loads(prompt.split("OWNER FACTS (confirmed)\n", 1)[1].split("\n\nPAGE", 1)[0])
    page = json.loads(prompt.split("PAGE (untrusted website content)\n", 1)[1])
    by_label = {f["label"]: f for f in facts}
    fills, covered = [], set()
    for f in page["fields"]:
        label = f.get("label") or ""
        if f["kind"] == "file":
            if "resume" in label.lower() and not f.get("value"):
                fills.append({"field_id": f["id"], "action": "upload_resume", "value": "", "fact_ids": []})
            continue
        if f["kind"] == "checkbox":
            if re.search(r"certify|agree", label, re.I) and f.get("value") != "checked":
                fills.append({"field_id": f["id"], "action": "check", "value": "", "fact_ids": []})
            continue
        for pat, fact_label in LABEL_MAP:
            if re.search(pat, label, re.I) and fact_label in by_label:
                fact = by_label[fact_label]
                covered.add(fact_label)
                if (f.get("value") or "").strip().lower() == str(fact["value"]).lower():
                    break
                action = "select" if f["kind"] in ("select", "radio", "combobox") else "fill"
                fills.append({"field_id": f["id"], "action": action, "value": fact["value"], "fact_ids": [fact["id"]]})
                break
    clicks, advance, kind = [], "", "none"
    buttons = page["buttons"]
    for b in buttons:
        if DISMISS.match(b["text"].strip()):
            clicks = [b["id"]]
    for pat, k in ((r"submit application", "submit"), (r"^next$|^continue$", "next"), (r"^apply", "apply")):
        hit = next((b for b in buttons if re.search(pat, b["text"].strip(), re.I)), None)
        if hit:
            advance, kind = hit["id"], k
            break
    text = page.get("page_text_excerpt") or ""
    if re.search(r"thank you for applying|received your application", text, re.I):
        page_kind = "confirmation"
    elif page["fields"]:
        page_kind = "application_form"
    elif kind == "apply":
        page_kind = "job_description"
    else:
        page_kind = "other"
    out = {"page_kind": page_kind, "fills": fills, "clicks": clicks, "pointer_clicks": [], "questions": [],
           "advance_button_id": advance, "advance_kind": kind, "blocker": "", "notes": ""}
    if "other_controls" in schema.get("properties", {}):
        # Questions visible in the page text that the field list doesn't have (seen on the screenshot).
        others = []
        for pat, fact_label in LABEL_MAP:
            if fact_label in covered or fact_label not in by_label:
                continue
            m = re.search(pat, text, re.I)
            if m:
                others.append({"label": m.group(0), "value": by_label[fact_label]["value"],
                               "fact_ids": [by_label[fact_label]["id"]], "write": False})
        out["other_controls"] = others
    return out


@dataclass
class Counters:
    planner_calls: int = 0
    agent_calls: int = 0
    other_calls: int = 0
    planner_ms: int = 0
    takeovers: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


def make_fake_run_task(counters: Counters, agent_decider):
    from app.apply.planner import SYSTEM_PROMPT

    async def fake_run_task(task, system, prompt, schema, job_id=None, timeout=240, images=None,
                            prefer_fallback=False):
        from app.ai.client import AIResult

        if system.startswith(SYSTEM_PROMPT[:60]):
            counters.planner_calls += 1
            return AIResult(fake_plan(prompt, schema), "test", "planner", "", 1)
        counters.other_calls += 1
        if "conflicts" in schema.get("properties", {}):
            return AIResult({"conflicts": []}, "test", "review", "", 1)
        if "answered" in schema.get("properties", {}):
            return AIResult({"answered": []}, "test", "second_look", "", 1)
        raise AssertionError(f"unexpected AI task: {system[:60]}")

    async def fake_agent_decider(system, prompt, images, prefer_fallback, job_id):
        counters.agent_calls += 1
        return await agent_decider(system, prompt, images, prefer_fallback, job_id)

    return fake_run_task, fake_agent_decider


@contextlib.contextmanager
def cdp_chromium(chromium: str):
    """A separate headless Chromium with DevTools on a free local port (like the browser container)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    profile = tempfile.mkdtemp(prefix="jp-test-profile-")
    proc = subprocess.Popen([chromium, "--headless=new", "--no-sandbox", "--disable-gpu",  # noqa: S603
                             f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--window-size=1100,800", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
            break
        time.sleep(0.2)
    try:
        yield port
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(5)


def seed(data_dir, job_url: str, agent: bool) -> int:
    from app.db import session_factory
    from app.models import Fact, Job, Resume
    from app.settings_store import AutomationSettings, put_setting

    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    sha = hashlib.sha256(pdf).hexdigest()
    from app.config import get_settings

    s = get_settings()
    s.resume_dir.mkdir(parents=True, exist_ok=True)
    (s.resume_dir / f"v1-{sha[:12]}.pdf").write_bytes(pdf)
    with session_factory()() as db:
        for sec, label, value in FACTS:
            db.add(Fact(section=sec, key=label.lower(), label=label, value=value, status="confirmed"))
        db.add(Resume(version=1, filename="Test Applicant Resume.pdf", sha256=sha, size_bytes=len(pdf),
                      is_active=True))
        job = Job(url=job_url, canonical_url=job_url, employer="Acme Health", title="IT Support Specialist")
        db.add(job)
        put_setting(db, "automation", AutomationSettings(paused=False, dry_run=False, auto_submit=True,
                                                         fit_check=False, browser_agent=agent))
        db.commit()
        return job.id


@dataclass
class RunResult:
    outcome: str
    submitted: bool
    seconds: float
    counters: Counters
    posted: dict
    events: list[dict]


async def run_application(monkeypatch, tmp_path, site, chromium: str, agent: bool, job_page: str = "job",
                          decider=None) -> RunResult:
    """Run one full application with the real worker. agent=False is the previous behavior."""
    import urllib.parse

    from app.apply import worker
    from app.config import get_settings
    from app.db import session_factory
    from app.models import Event

    counters = Counters()
    fake_run_task, fake_decider = make_fake_run_task(counters, decider or HeuristicDecider())
    monkeypatch.setattr(worker, "run_task", fake_run_task)
    monkeypatch.setattr("app.apply.agent.ai_decider", fake_decider)

    async def fake_ask(job_id, text, options, kind="fact", photo=None):
        counters.questions.append(text[:300])
        return None  # the owner never answers in tests: the run stops (counted as human needed)

    async def fake_takeover(job_id, reason, photo):
        counters.takeovers.append(reason[:200])
        raise worker.Stopped(f"takeover needed: {reason}")

    monkeypatch.setattr(worker, "_ask", fake_ask)
    monkeypatch.setattr(worker, "_takeover", fake_takeover)
    site.server.posts.clear()
    with cdp_chromium(chromium) as port:
        monkeypatch.setenv("JOBPILOT_BROWSER_CDP_HOST", "127.0.0.1")
        monkeypatch.setenv("JOBPILOT_BROWSER_CDP_PORT", str(port))
        get_settings.cache_clear()
        job_id = seed(tmp_path, site.at(job_page), agent)
        t0 = time.monotonic()
        res = await asyncio.wait_for(worker._run_locked(job_id, force=True), 600)
        secs = time.monotonic() - t0
    posted = {}
    if site.server.posts:
        posted = {k: v[0] for k, v in urllib.parse.parse_qs(site.server.posts[-1][1]).items()}
    with session_factory()() as db:
        events = [{"type": e.type, **(e.data or {})} for e in db.query(Event).filter(Event.job_id == job_id)
                  .order_by(Event.id)]
    return RunResult(res["outcome"], res["submitted"], secs, counters, posted, events)
