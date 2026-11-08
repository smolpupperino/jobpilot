"""Read verification codes / links from the job-search Gmail.

Two ways in:
  - "browser" (default): the owner signs in to Gmail once inside JobPilot's own
    browser. We read unread mail through Gmail's Atom feed and "show original"
    pages, fetched from a Gmail tab in that browser (so its login cookies are used).
    No 2-Step Verification or App password needed.
  - "imap": if JOBPILOT_GMAIL_APP_PASSWORD is set, plain IMAP.

Strict on purpose (see SPEC section 4): a code or link is only used when
  - the worker is actively waiting for one (it calls wait_for_verification),
  - the email arrived after the wait started (and is < 10 minutes old),
  - the sender or the link belongs to the same job site / ATS as the page.
Email content is untrusted: we only pull out a short code or a link whose host
matches the site, never act on anything else in it.
"""

import asyncio
import email
import email.policy
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit

from app.config import get_settings
from app.jobs_util import detect_ats

MAX_AGE = timedelta(minutes=10)
CODE_HINT = re.compile(r"(code|verif|one[- ]time|passcode|pin|otp)", re.I)
CODE_TOKEN = re.compile(r"\b([0-9]{4,8}|[A-Z0-9]{6,8})\b")
LINK_HINT = re.compile(r"(verif|confirm|activat|validat|token|registration)", re.I)
HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
BARE_URL = re.compile(r"https://[^\s<>\"')]+")

# Same family = same ATS vendor even across different domains (Workday sends from
# myworkday.com but links to myworkdayjobs.com).
FAMILY_EXTRA = {
    "workday": ("myworkday.com", "workday.com", "myworkdayjobs.com"),
    "taleo": ("oracle.com", "taleo.net"),
    "successfactors": ("sap.com", "successfactors.com", "successfactors.eu"),
    "oracle": ("oracle.com", "oraclecloud.com"),
}


@dataclass
class Message:
    received: datetime
    sender: str
    subject: str
    text: str
    links: list[str]


def _base(host: str) -> str:
    parts = (host or "").lower().split(".")
    return ".".join(parts[-2:])


def _family(host: str) -> set[str]:
    ats = detect_ats(f"https://{host}")
    fam = {_base(host)}
    if ats in FAMILY_EXTRA:
        fam |= set(FAMILY_EXTRA[ats])
    return fam


def related(site_host: str, other_host: str) -> bool:
    if not site_host or not other_host:
        return False
    return bool(_family(site_host) & _family(other_host))


def _parse(raw: bytes, fallback_time: datetime) -> Message:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        received = parsedate_to_datetime(msg["Date"]).astimezone(UTC)
    except (TypeError, ValueError):
        received = fallback_time
    texts, links = [], []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            try:
                body = part.get_content()
            except (LookupError, ValueError):
                continue
            if part.get_content_type() == "text/html":
                links += [unescape(h) for h in HREF.findall(body)]
                body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
                body = unescape(re.sub(r"<[^>]+>", " ", body))
            texts.append(body)
    text = re.sub(r"\s+", " ", " ".join(texts))[:20000]
    links += BARE_URL.findall(text)
    return Message(received, parseaddr(msg.get("From", ""))[1].lower(), str(msg.get("Subject", "")), text,
                   list(dict.fromkeys(links)))


def extract_code(m: Message) -> str | None:
    text = f"{m.subject} {m.text}"
    for hint in CODE_HINT.finditer(text):
        window = text[hint.start(): hint.start() + 160]
        for tok in CODE_TOKEN.findall(window):
            if not re.fullmatch(r"(19|20)\d\d", tok):  # skip years
                return tok
    return None


def extract_link(m: Message, site_host: str) -> str | None:
    for link in m.links:
        parts = urlsplit(link)
        if parts.scheme == "https" and related(site_host, parts.hostname or "") and LINK_HINT.search(link):
            return link
    return None


def _from_site(m: Message, site_host: str) -> bool:
    sender_host = m.sender.split("@")[-1]
    return related(site_host, sender_host) or any(
        related(site_host, urlsplit(lnk).hostname or "") for lnk in m.links)


def _fetch_imap(since: datetime) -> list[Message]:
    s = get_settings()
    if not (s.gmail_address and s.gmail_app_password):
        raise RuntimeError("Gmail isn't connected (run scripts/setup-gmail.sh)")
    out: list[Message] = []
    with imaplib.IMAP4_SSL(s.gmail_imap_host, timeout=30) as imap:
        imap.login(s.gmail_address, s.gmail_app_password)
        for box in ("INBOX", '"[Gmail]/Spam"'):
            if imap.select(box, readonly=True)[0] != "OK":
                continue
            day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
            ok, data = imap.search(None, f"(SINCE {day})")
            if ok != "OK":
                continue
            for num in data[0].split()[-25:]:
                ok, parts = imap.fetch(num, "(RFC822)")
                if ok == "OK" and parts and isinstance(parts[0], tuple):
                    out.append(_parse(parts[0][1], datetime.now(UTC)))
    return out


def _fetch_dev(since: datetime) -> list[Message]:
    out = []
    for p in sorted(Path(get_settings().dev_mailbox_dir).glob("*.eml")):
        out.append(_parse(p.read_bytes(), datetime.fromtimestamp(p.stat().st_mtime, UTC)))
    return out


class GmailNotSignedIn(Exception):
    pass


GMAIL = "https://mail.google.com/mail/u/0/"
ATOM_NS = {"a": "http://purl.org/atom/ns#"}


async def _gmail_tab(context):
    for pg in context.pages:
        if pg.url.startswith("https://mail.google.com/"):
            return pg, False
    pg = await context.new_page()
    await pg.goto(GMAIL, wait_until="domcontentloaded", timeout=60000)
    return pg, True


async def fetch_browser(context, since: datetime) -> list[Message]:
    """Unread mail via the Gmail tab in JobPilot's browser (same-origin fetches)."""
    import xml.etree.ElementTree as ET  # noqa: S405  (Google's own feed, parsed defensively)

    page, opened = await _gmail_tab(context)
    try:
        if not page.url.startswith("https://mail.google.com/"):
            raise GmailNotSignedIn("Gmail isn't signed in inside JobPilot's browser")
        atom = await page.evaluate(
            "fetch('/mail/u/0/feed/atom', {credentials: 'include'}).then(r => r.ok ? r.text() : '')")
        if "<feed" not in (atom or ""):
            raise GmailNotSignedIn("Gmail isn't signed in inside JobPilot's browser")
        root = ET.fromstring(atom)  # noqa: S314
        out = []
        for entry in root.findall("a:entry", ATOM_NS)[:15]:
            issued = entry.findtext("a:issued", "", ATOM_NS) or entry.findtext("a:modified", "", ATOM_NS)
            try:
                received = datetime.fromisoformat(issued.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                continue
            if received < since - timedelta(seconds=60):
                continue
            link = (entry.find("a:link", ATOM_NS).attrib.get("href", "")
                    if entry.find("a:link", ATOM_NS) is not None else "")
            mid = re.search(r"message_id=([0-9a-f]+)", link)
            msg = None
            if mid:
                html = await page.evaluate(
                    "id => fetch('/mail/u/0/?view=om&th=' + id, {credentials: 'include'})"
                    ".then(r => r.ok ? r.text() : '')", mid.group(1))
                raw = re.search(r"<pre[^>]*>(.*?)</pre>", html or "", re.S)
                if raw:
                    msg = _parse(unescape(raw.group(1)).encode(), received)
            if msg is None:  # fall back to the feed's own snippet
                sender = entry.findtext("a:author/a:email", "", ATOM_NS).lower()
                text = f"{entry.findtext('a:title', '', ATOM_NS)} {entry.findtext('a:summary', '', ATOM_NS)}"
                msg = Message(received, sender, entry.findtext("a:title", "", ATOM_NS), text,
                              BARE_URL.findall(text))
            msg.received = received
            out.append(msg)
        return out
    finally:
        if opened:
            await page.close()


async def gmail_browser_status(context) -> bool:
    try:
        await fetch_browser(context, datetime.now(UTC))
        return True
    except GmailNotSignedIn:
        return False


def fetch_since(since: datetime) -> list[Message]:
    msgs = _fetch_dev(since) if get_settings().dev_mailbox_dir else _fetch_imap(since)
    return _fresh(msgs, since)


def _fresh(msgs: list[Message], since: datetime) -> list[Message]:
    now = datetime.now(UTC)
    return [m for m in msgs if m.received >= since - timedelta(seconds=60) and now - m.received <= MAX_AGE]


def mode() -> str:
    s = get_settings()
    if s.dev_mailbox_dir:
        return "dev"
    return "imap" if s.gmail_app_password else "browser"


async def wait_for_verification(site_host: str, since: datetime, want: str, timeout: int = 240,
                                should_stop=None, context=None) -> str | None:
    """Poll for a fresh email from this site; return a code ("code") or link ("link")."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if should_stop:
            should_stop()
        if mode() == "browser":
            msgs = _fresh(await fetch_browser(context, since), since)
        else:
            msgs = await asyncio.to_thread(fetch_since, since)
        for m in sorted(msgs, key=lambda m: m.received, reverse=True):
            if not _from_site(m, site_host):
                continue
            found = extract_code(m) if want == "code" else extract_link(m, site_host)
            if found:
                return found
        await asyncio.sleep(8)
    return None
