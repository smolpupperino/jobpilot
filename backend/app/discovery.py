"""Job discovery from the job Gmail.

Every `discovery_interval_minutes` (and on Telegram /scan), read unread mail in the job Gmail
(inside JobPilot's browser, or IMAP/dev mailbox if configured) and:

1. Job-alert emails (LinkedIn, Indeed, company career sites, ...): add the jobs they link to
   as new leads, deduped by canonical URL.
2. Employer replies to applications we sent: add them to that job's timeline and move its
   status (received -> Acknowledged, rejection -> Rejected, interview -> Interview).
   Interviews and assessments get an immediate Telegram alert.

Rules only, no AI (SPEC section 5). Email is untrusted:
- Links are never opened from here. Only links that ARE a job posting on a known job board
  or ATS are kept (tracking redirects like Glassdoor/ZipRecruiter click links are dropped).
- Text from emails is stored as data (titles, subjects) and shown as plain text.
- A reply is linked to a job only when exactly one tracked application matches it.
"""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import mail
from app.db import session_factory
from app.events import log_event
from app.jobs_util import InvalidJobURL, canonicalize_url, detect_ats
from app.models import Job
from app.settings_store import AutomationSettings, get_raw, get_setting, put_raw
from app.telegram.bot import notify

log = logging.getLogger("jobpilot.discovery")

STATE_KEY = "discovery_state"
MAX_SEEN = 1000
MAX_LEADS_PER_EMAIL = 30

# --- Is it a job alert? -------------------------------------------------------

BOARD_DOMAINS = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
                 "dice.com", "simplyhired.com", "careerbuilder.com", "builtin.com", "wellfound.com",
                 "usajobs.gov", "clearancejobs.com", "talent.com", "jobright.ai"}
ALERT_SUBJECT = re.compile(
    r"job alert|jobs? for you|\bnew jobs?\b|recommended (jobs?|for you)|jobs? (that )?match|matching jobs"
    r"|jobs? you (may|might)|(is|are) hiring|jobs? near|new opportunit|jobs? (were )?posted|job matches"
    r"|similar jobs|new positions?|openings? (for|that)", re.I)
STATUS_SUBJECT = re.compile(
    r"your application|application (received|submitted|status|update|for)|thank(s| you) for (applying|your "
    r"interest|your application)|interview|assessment|verif|confirm your|password|sign[- ]?in|security code",
    re.I)


def _base(host: str) -> str:
    return ".".join((host or "").lower().split(".")[-2:])


def is_alert(m: mail.Message) -> bool:
    if STATUS_SUBJECT.search(m.subject):
        return False
    local, _, domain = m.sender.partition("@")
    if _base(domain) in BOARD_DOMAINS:
        return bool(ALERT_SUBJECT.search(m.subject)) or "job" in local
    return bool(ALERT_SUBJECT.search(m.subject))


# --- Job links in an alert ----------------------------------------------------

JOB_PATH = re.compile(r"/jobs?/|/job[-_]?details|/requisition|/posting|/position|/opening|/careers?/.+\d"
                      r"|[?&](gh_jid|jobid|job_id|jobreq|req(uisition)?id)=|/[0-9a-f-]{20,}|/\d{4,}", re.I)
# Whole path segments only, so "Help-Desk-Tech" isn't mistaken for a help page.
NOT_JOB = re.compile(r"unsubscribe|/(preferences?|settings|privacy|log-?in|sign-?in|alerts?|manage\w*"
                     r"|saved\w*|search|help|terms|cookies?|account\w*|userhome)(?=/|$|[.?#])", re.I)
GENERIC_TEXT = re.compile(r"^(view( (the )?job| details| all.*)?|apply( now)?|easy apply|see (all|more).*"
                          r"|learn more|more jobs.*|details|unsubscribe|here|click here|open|new|promoted"
                          r"|actively recruiting"
                          r"|\d+ (new )?jobs?.*|jobs?)$", re.I)
LINKEDIN_JOB = re.compile(r"/(?:comm/)?jobs/view/(?:[^/?]*-)?(\d{6,})")
INDEED_JK = re.compile(r"^[0-9a-f]{8,24}$", re.I)

# Employer name from the posting URL: (host pattern, where the slug is).
SLUG_HOSTS = [
    (re.compile(r"^(job-)?boards(\.eu)?\.greenhouse\.io$"), "path"),
    (re.compile(r"^jobs(\.eu)?\.lever\.co$"), "path"),
    (re.compile(r"^jobs\.ashbyhq\.com$"), "path"),
    (re.compile(r"^(jobs|careers)\.smartrecruiters\.com$"), "path"),
    (re.compile(r"^jobs\.jobvite\.com$"), "path"),
    (re.compile(r"^([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com$"), "host"),
    (re.compile(r"^(?:careers-)?([a-z0-9-]+)\.icims\.com$"), "host"),
    (re.compile(r"^([a-z0-9-]+)\.bamboohr\.com$"), "host"),
    (re.compile(r"^([a-z0-9-]+)\.taleo\.net$"), "host"),
    (re.compile(r"^([a-z0-9-]+)\.applytojob\.com$"), "host"),
]
NOT_SLUG = {"embed", "jobs", "careers", "career", "www", "job", "apply", "en-us", "us", "external"}


@dataclass
class Lead:
    url: str
    canonical: str
    title: str = ""
    employer: str = ""
    source: str = "email"
    ats: str = "unknown"


def _pretty(slug: str) -> str:
    slug = re.sub(r"[-_]+", " ", slug).strip()
    return slug.title() if slug.islower() else slug


def employer_from_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for pat, where in SLUG_HOSTS:
        m = pat.match(host)
        if not m:
            continue
        if where == "host":
            slug = m.group(1)
        else:
            segs = [s for s in parts.path.split("/") if s]
            slug = segs[0] if segs else ""
        return "" if not slug or slug.lower() in NOT_SLUG else _pretty(slug)[:100]
    return ""


def lead_from_link(href: str) -> Lead | None:
    href = href.strip()
    parts = urlsplit(href)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        m = LINKEDIN_JOB.search(parts.path)
        if not m:
            return None
        url = f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
        return Lead(url, canonicalize_url(url), source="linkedin_alert", ats="linkedin")
    if host == "indeed.com" or host.endswith(".indeed.com"):
        jk = next((v for k, v in parse_qsl(parts.query) if k.lower() == "jk" and INDEED_JK.match(v)), None)
        if not jk:
            return None
        url = f"https://www.indeed.com/viewjob?jk={jk}"
        return Lead(url, canonicalize_url(url), ats="indeed")
    ats = detect_ats(href)
    if ats in ("unknown", "linkedin", "indeed"):
        return None
    tail = parts.path + ("?" + parts.query if parts.query else "")
    if not JOB_PATH.search(tail) or NOT_JOB.search(parts.path):
        return None
    try:
        canon = canonicalize_url(href)
    except InvalidJobURL:
        return None
    return Lead(canon, canon, employer=employer_from_url(canon), ats=ats)


def _title(text: str) -> str:
    t = re.sub(r"\s+", " ", text or "").strip(" -|·•")
    if not 3 <= len(t) <= 150 or GENERIC_TEXT.match(t) or not re.search(r"[A-Za-z]{2}", t):
        return ""
    return t


def _employer_after(title: str, text: str) -> str:
    """LinkedIn-style "Title Company · Location": the words between the title and the first dot."""
    if not title:
        return ""
    i = text.find(title)
    if i < 0:
        return ""
    m = re.match(r"\s*([^·|•]{2,80}?)\s*[·•]", text[i + len(title): i + len(title) + 120])
    if not m:
        return ""
    emp = m.group(1).strip()
    return "" if re.search(r"https?:|\d{3,}", emp) else emp


def leads_from_alert(m: mail.Message) -> list[Lead]:
    found: dict[str, Lead] = {}
    pairs = list(m.anchors) + [(lnk, "") for lnk in m.links]
    for href, text in pairs:
        lead = lead_from_link(href)
        if lead is None:
            continue
        lead = found.setdefault(lead.canonical, lead)
        if not lead.title:
            lead.title = _title(text)
        if len(found) >= MAX_LEADS_PER_EMAIL:
            break
    for lead in found.values():
        if lead.source == "linkedin_alert" and not lead.employer:
            lead.employer = _employer_after(lead.title, m.text)
    return list(found.values())


# --- Employer replies ---------------------------------------------------------

REJECTED = re.compile(
    r"unfortunately|not (to )?(be )?mov(e|ing) forward|decided to (move forward|pursue|proceed|go) with other"
    r"|other candidates|no longer (being )?considered|(position|role) has been filled|not (been )?selected"
    r"|regret to inform|will not be (moving|proceeding)", re.I)
INTERVIEW = re.compile(
    r"invit\w* (you )?(to|for) (an? |your )?(interview|phone|video|call)"
    r"|schedule (an? |your |some )?(interview|call|time|phone)|interview (invitation|request)"
    r"|your availability|phone screen|would (like|love) to (speak|talk|chat|meet) with you"
    r"|next step is (an? )?(interview|call)", re.I)
ASSESSMENT = re.compile(r"assessment|skills test|hirevue|codility|hackerrank|testgorilla|criteria corp", re.I)
RECEIVED = re.compile(
    r"(received|receipt of) your application"
    r"|thank(s| you) for (applying|your application|your interest|submitting)"
    r"|application (has been |was )?(received|submitted)|we('ve| have) received", re.I)
REPLY_STATUSES = ("submitted_confirmed", "submitted_uncertain", "acknowledged", "interview")
COMPANY_SUFFIX = re.compile(r"[,.]?\s+(inc|llc|ltd|corp|corporation|co|company|group|plc|lp|llp)\.?$", re.I)


def classify_reply(m: mail.Message) -> str:
    text = f"{m.subject} {m.text[:5000]}"
    if REJECTED.search(text):
        return "rejected"
    if INTERVIEW.search(text):
        return "interview"
    if ASSESSMENT.search(text):
        return "assessment"
    if RECEIVED.search(text):
        return "acknowledged"
    return "unclear"


def _norm_employer(name: str) -> str:
    return COMPANY_SUFFIX.sub("", (name or "").strip()).lower()


def match_job(db: Session, m: mail.Message) -> Job | None:
    """The one application this email is about, or None if zero or several match."""
    jobs = db.scalars(select(Job).where(Job.status.in_(REPLY_STATUSES))).all()
    haystack = f"{m.sender} {m.subject} {m.text[:5000]}".lower()
    sender_host = m.sender.partition("@")[2]
    by_name, by_host = [], []
    for j in jobs:
        name = _norm_employer(j.employer)
        if len(name) >= 3 and re.search(r"\b" + re.escape(name) + r"\b", haystack):
            by_name.append(j)
        elif sender_host and mail.related(urlsplit(j.url).hostname or "", sender_host):
            by_host.append(j)
    if len(by_name) == 1:
        return by_name[0]
    if not by_name and len(by_host) == 1:
        return by_host[0]
    return None


NEXT_STATUS = {"rejected": "rejected", "interview": "interview", "acknowledged": "acknowledged"}


def _apply_reply(db: Session, job: Job, kind: str, m: mail.Message) -> bool:
    """Timeline event + status move. Returns True if the status changed."""
    log_event(db, "system", "email.received", job_id=job.id, kind=kind, subject=m.subject[:200],
              sender=m.sender[:200])
    new = NEXT_STATUS.get(kind)
    if new is None or job.status == new:
        return False
    if new == "acknowledged" and job.status not in ("submitted_confirmed", "submitted_uncertain"):
        return False  # never move an interview back to "acknowledged"
    old, job.status = job.status, new
    log_event(db, "system", "job.status_changed", job_id=job.id, before=old, after=new, via="email")
    return True


# --- Processing ---------------------------------------------------------------

def _key(m: mail.Message) -> str:
    if m.msg_id:
        return m.msg_id
    raw = f"{m.sender}|{m.subject}|{m.received.isoformat()}"
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def process(db: Session, msgs: list[mail.Message]) -> dict:
    """Handle new messages (already-seen ones are skipped). Caller commits."""
    state = get_raw(db, STATE_KEY) or {}
    seen: list[str] = list(state.get("seen", []))
    seen_set = set(seen)
    added: list[dict] = []
    replies: list[dict] = []
    alerts = 0
    for m in sorted(msgs, key=lambda x: x.received):
        k = _key(m)
        if k in seen_set:
            continue
        seen.append(k)
        seen_set.add(k)
        if is_alert(m):
            alerts += 1
            for lead in leads_from_alert(m):
                if db.scalar(select(Job.id).where(Job.canonical_url == lead.canonical)):
                    continue
                job = Job(url=lead.url, canonical_url=lead.canonical, title=lead.title[:300],
                          employer=lead.employer[:300], ats=lead.ats, source=lead.source, status="new")
                db.add(job)
                db.flush()
                log_event(db, "system", "job.added", job_id=job.id, ats=job.ats, source=job.source,
                          via="job_alert_email")
                added.append({"id": job.id, "title": job.title, "employer": job.employer, "ats": job.ats})
            continue
        job = match_job(db, m)
        if job is None:
            continue
        kind = classify_reply(m)
        changed = _apply_reply(db, job, kind, m)
        replies.append({"job_id": job.id, "kind": kind, "changed": changed, "subject": m.subject[:150],
                        "label": f"{job.title or 'Job'} at {job.employer or 'employer'}"})
    state["seen"] = seen[-MAX_SEEN:]
    put_raw(db, STATE_KEY, state)
    return {"alerts": alerts, "added": added, "replies": replies}


def report_text(result: dict) -> str:
    lines: list[str] = []
    urgent = [r for r in result["replies"] if r["kind"] in ("interview", "assessment")]
    for r in urgent:
        what = "Interview request" if r["kind"] == "interview" else "Assessment to complete"
        lines += [f"{what}: {r['label']}", f'Email: "{r["subject"]}"', ""]
    other = [r for r in result["replies"] if r["kind"] in ("rejected", "acknowledged")]
    if other:
        lines.append("Employer emails:")
        names = {"rejected": "rejected", "acknowledged": "application received"}
        lines += [f"- {r['label']}: {names[r['kind']]}" for r in other]
        lines.append("")
    added = result["added"]
    if added:
        auto = [a for a in added if a["ats"] != "linkedin"]
        li = len(added) - len(auto)
        lines.append(f"Found {len(added)} new jobs in job-alert emails.")
        for a in auto[:15]:
            lines.append(f"- {a['title'] or 'Job'}" + (f" at {a['employer']}" if a["employer"] else ""))
        if len(auto) > 15:
            lines.append(f"- ...and {len(auto) - 15} more")
        if li:
            lines.append(f"{li} are LinkedIn links: JobPilot won't automate LinkedIn, so apply to those "
                         "yourself or paste the company's own job link on the Jobs page.")
        if auto:
            lines.append("Send /batch to apply to them.")
    return "\n".join(lines).strip()


# --- Fetching and the background loop -----------------------------------------

async def _fetch(seen: set[str]) -> list[mail.Message]:
    if mail.mode() == "browser":
        from app.apply.browser import cdp

        async with cdp() as (_b, ctx):
            return await mail.fetch_browser(ctx, None, mail.ALL_FEEDS, skip_ids=seen, limit=20)
    since = datetime.now(UTC) - timedelta(days=3)
    if mail.mode() == "dev":
        return await asyncio.to_thread(mail._fetch_dev, since)
    return await asyncio.to_thread(mail._fetch_imap, since)


def _db():
    return session_factory()()


async def scan() -> dict:
    """One scan. Waits for the browser if an application is using it."""
    from app.apply.browser import browser_lock

    with _db() as db:
        seen = set((get_raw(db, STATE_KEY) or {}).get("seen", []))
    async with browser_lock:
        msgs = await _fetch(seen)
    with _db() as db:
        result = process(db, msgs)
    from app import job_details

    for a in result["added"]:
        if a["ats"] != "linkedin":
            job_details.fill_later(a["id"])
    with _db() as db:
        state = get_raw(db, STATE_KEY) or {}
        state["last_scan"] = datetime.now(UTC).isoformat()
        state["last_result"] = {"alerts": result["alerts"], "added": len(result["added"]),
                                "replies": len(result["replies"]), "error": None}
        put_raw(db, STATE_KEY, state)
        if result["added"] or result["replies"]:
            log_event(db, "system", "discovery.scan", alerts=result["alerts"], added=len(result["added"]),
                      replies=len(result["replies"]))
        db.commit()
    return result


def _record_error(err: str) -> bool:
    """Save the error; returns True if the owner hasn't been told about it today."""
    today = datetime.now(UTC).date().isoformat()
    with _db() as db:
        state = get_raw(db, STATE_KEY) or {}
        state["last_scan"] = datetime.now(UTC).isoformat()
        state["last_result"] = {"error": err}
        first = state.get("warned_date") != today
        state["warned_date"] = today
        put_raw(db, STATE_KEY, state)
        db.commit()
    return first


async def scan_and_report(manual: bool = False) -> None:
    try:
        result = await scan()
    except mail.GmailNotSignedIn:
        if _record_error("Gmail isn't signed in inside JobPilot's browser") or manual:
            notify("I can't read the job Gmail: it isn't signed in inside JobPilot's browser. "
                   "Settings -> Job-site accounts & Gmail -> Sign in.")
        return
    except Exception as e:  # noqa: BLE001
        log.warning("email scan failed: %s", type(e).__name__)
        if _record_error(f"Scan failed ({type(e).__name__})") and manual:
            notify(f"Checking email failed ({type(e).__name__}).")
        return
    text = report_text(result)
    if text:
        notify(text)
    elif manual:
        notify("No new job-alert or employer emails.")


def status() -> dict:
    with _db() as db:
        state = get_raw(db, STATE_KEY) or {}
    last = state.get("last_scan")
    try:  # naive UTC, like every other timestamp the dashboard shows
        last = datetime.fromisoformat(last).astimezone(UTC).replace(tzinfo=None).isoformat() if last else None
    except ValueError:
        last = None
    return {"last_scan": last, "last_result": state.get("last_result")}


def _due(a: AutomationSettings, last: str | None, now: datetime) -> bool:
    if not a.discovery or a.paused:
        return False
    if not last:
        return True
    try:
        return now - datetime.fromisoformat(last) >= timedelta(minutes=a.discovery_interval_minutes)
    except ValueError:
        return True


async def run_loop() -> None:
    """Background loop (app lifespan). Scans when due and the browser is free."""
    from app.apply.browser import browser_lock

    await asyncio.sleep(30)
    while True:
        try:
            with _db() as db:
                a = get_setting(db, "automation", AutomationSettings)
                last = (get_raw(db, STATE_KEY) or {}).get("last_scan")
            if _due(a, last, datetime.now(UTC)) and not browser_lock.locked():
                await scan_and_report()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("discovery loop error")
        await asyncio.sleep(60)
