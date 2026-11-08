"""The application worker: drives the browser through one job application.

Loop, one page at a time:
  read the page -> AI plans -> code checks the plan -> ask owner anything missing
  -> fill -> advance. Before the final submit it ALWAYS stops: in dry-run it just
  reports; otherwise it sends a screenshot to Telegram and waits for "Submit".
  It only marks a job submitted after seeing the click go through, and only
  "confirmed" when the next page looks like a confirmation.

One application at a time. The owner can stop it any time (dashboard Stop, or
Pause everything / Telegram /pause), and can take over in the browser (noVNC).
"""

import asyncio
import logging
import re
import shutil
import socket
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.ai.client import AIUnavailable, run_task
from app.apply.page import PageState, extract, locator, read_combo_options
from app.apply.planner import PLAN_SCHEMA, SYSTEM_PROMPT, Action, CheckedPlan, build_prompt, check_plan
from app.config import get_settings
from app.db import session_factory, utcnow
from app.events import log_event
from app.mail import GmailNotSignedIn, wait_for_verification
from app.models import Fact, Job, OwnerQuestion, Resume
from app.questions import create_question
from app.settings_store import AutomationSettings, get_setting
from app.telegram.bot import notify
from app.vault import VaultError, get_account, get_or_create_account, password_of, site_key

log = logging.getLogger("jobpilot.apply")

MAX_STEPS = 30
CONFIRM_TEXT = re.compile(
    r"(thank you for (applying|your application|your interest)|application (has been |was )?"
    r"(received|submitted|complete)|we('ve| have) received your application|successfully submitted)",
    re.I,
)


ACCOUNT_ACTIONS = {"account_email", "account_password"}


class ApplyError(Exception):
    pass


class Stopped(Exception):
    pass


class _Skip(Exception):
    pass


_state: dict[str, Any] = {"running": False, "job_id": None, "step": 0, "message": "", "started_at": None}
_task: asyncio.Task | None = None
_stop = asyncio.Event()


def status() -> dict[str, Any]:
    return {**_state, "view_url": get_settings().browser_view_url or None}


def _set(msg: str, **kw: Any) -> None:
    _state.update(message=msg, **kw)
    log.info("apply: %s", msg)


def _db():
    return session_factory()()


def _event(job_id: int, type_: str, **data: Any) -> None:
    with _db() as db:
        log_event(db, "system", type_, job_id=job_id, **data)
        db.commit()


def _job_status(job_id: int, new: str) -> None:
    with _db() as db:
        j = db.get(Job, job_id)
        if j and j.status != new:
            old, j.status = j.status, new
            log_event(db, "system", "job.status_changed", job_id=job_id, before=old, after=new)
            db.commit()


def _automation() -> AutomationSettings:
    with _db() as db:
        return get_setting(db, "automation", AutomationSettings)


def _check_stop() -> None:
    if _stop.is_set():
        raise Stopped("Stopped from the dashboard")
    if _automation().paused:
        raise Stopped("Automation was paused")


def _facts() -> list[dict]:
    with _db() as db:
        rows = db.scalars(select(Fact).where(Fact.status == "confirmed")).all()
        return [{"id": f.id, "section": f.section, "label": f.label, "value": f.value}
                for f in rows if f.value]


def _prepare_resume() -> Path:
    """Copy the active resume to upload/ under its original filename, so employers see
    "Your Name Resume.pdf" instead of an internal name. The original stays read-only."""
    s = get_settings()
    with _db() as db:
        r = db.scalar(select(Resume).where(Resume.is_active.is_(True)))
        if r is None:
            raise ApplyError("No active resume. Upload one on the Resume page first.")
        src = s.resume_dir / f"v{r.version}-{r.sha256[:12]}.pdf"
        name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", Path(r.filename).name) or "Resume.pdf"
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
    s.upload_dir.mkdir(parents=True, exist_ok=True)
    for old in s.upload_dir.glob("*.pdf"):
        old.unlink()
    dst = s.upload_dir / name
    shutil.copyfile(src, dst)
    return dst


async def _screenshot(page, job_id: int, step: int, label: str, full: bool = False) -> str | None:
    s = get_settings()
    rel = Path("runs") / f"job{job_id}" / f"{int(time.time())}-{step:02d}-{label}.png"
    path = s.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(path), full_page=full, timeout=20000)
        return str(rel)
    except Exception as e:  # noqa: BLE001
        log.warning("screenshot failed: %s", e)
        return None


COOKIE_DECLINE = re.compile(r"^(decline( all)?|reject( all)?( cookies)?|(only )?necessary( cookies)?( only)?"
                            r"|use necessary cookies only)$", re.I)
COOKIE_ACCEPT = re.compile(r"^(accept( all)?( cookies)?|i accept|got it|ok(ay)?)$", re.I)


async def _dismiss_cookies(page) -> None:
    """Close cookie banners so they don't cover buttons. Prefer declining."""
    for pattern in (COOKIE_DECLINE, COOKIE_ACCEPT):
        btn = page.get_by_role("button", name=pattern)
        try:
            if await btn.count() and await btn.first.is_visible():
                await btn.first.click(timeout=3000)
                await asyncio.sleep(0.5)
                return
        except Exception:  # noqa: BLE001, S112
            continue


async def _settle(page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:  # noqa: BLE001, S110  (busy pages never go idle; that's fine)
        pass
    await asyncio.sleep(1.0)
    await _wait_while_busy(page)
    await _dismiss_cookies(page)


# "Is the site still working?": a visible spinner/progress bar, or a near-empty page
# that says it's processing/loading. Long pages that merely mention "loading" don't count.
BUSY_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const spin = document.querySelectorAll('[role=progressbar], [aria-busy=true], .MuiCircularProgress-root, '
    + '.spinner, .loading-spinner, .loader, [class*=Spinner], [class*=spinner]');
  // A progress bar with a value ("step 4 of 9") isn't loading; only open-ended spinners count.
  const loading = e => !e.hasAttribute('aria-valuenow') && !/(^|[^n])determinate/i.test(String(e.className));
  if (Array.from(spin).some(e => vis(e) && loading(e))) return true;
  const t = (document.body ? document.body.innerText : '').trim();
  return t.length < 600 && /processing|loading|please wait|uploading|parsing|one moment/i.test(t);
}"""


async def _wait_while_busy(page, max_seconds: float = 90) -> bool:
    """Wait while the page shows it's still working. Returns True if it was busy at all."""
    deadline = time.monotonic() + max_seconds
    was_busy = False
    while time.monotonic() < deadline:
        try:
            busy = await page.evaluate(BUSY_JS)
        except Exception:  # noqa: BLE001  (navigating mid-check: give it a moment)
            busy = True
        if not busy:
            break
        if not was_busy:
            _set(f"Step {_state.get('step', 0)}: waiting for the site to finish loading")
        was_busy = True
        _check_stop()
        await asyncio.sleep(2.0)
    if was_busy:
        await asyncio.sleep(1.0)
    return was_busy


async def _wait_answer(qid: int, job_id: int) -> str | None:
    """Wait for the owner to answer a question. Returns the answer, or None on timeout."""
    deadline = time.monotonic() + get_settings().owner_wait_minutes * 60
    _job_status(job_id, "waiting_for_owner")
    try:
        while time.monotonic() < deadline:
            _check_stop()
            with _db() as db:
                q = db.get(OwnerQuestion, qid)
                if q is None or q.status != "open":
                    return q.answer if q else None
            await asyncio.sleep(2)
        return None
    finally:
        _job_status(job_id, "applying")


async def _ask(job_id: int, text: str, options: list[str], kind: str = "fact",
               photo: str | None = None) -> str | None:
    with _db() as db:
        q = create_question(db, text, options, job_id=job_id, kind=kind, photo_path=photo)
        db.commit()
        qid = q.id
    return await _wait_answer(qid, job_id)


async def _takeover(job_id: int, reason: str, photo: str | None) -> str | None:
    """Ask the owner for help. They can reply in plain words (returned as guidance for
    the AI), or fix it in the browser and tap Continue (returns None)."""
    view = get_settings().browser_view_url
    _set(f"Waiting for you on Telegram: {reason[:80]}")
    text = (f"I'm stuck: {reason}\n\nReply in your own words to tell me what to do (for example "
            "\"click I acknowledge and confirm\"), or fix it in the browser and tap Continue."
            + (f"\nBrowser: {view}" if view else ""))
    ans = await _ask(job_id, text, ["Continue", "Cancel"], kind="takeover", photo=photo)
    if ans == "Continue":
        return None
    if not ans or ans == "Cancel":
        raise Stopped("Owner cancelled" if ans else "No answer from owner in time")
    _event(job_id, "apply.guidance", text=ans[:300])
    return ans[:1000]


STUCK_HINT = ("Your previous plan for this page made no progress (nothing to fill, no button to press, "
              "or the page didn't change). Look at the attached screenshot and the buttons list again, "
              "and choose the action that moves the application forward.")


def _no_progress(plan: CheckedPlan) -> bool:
    return (not plan.actions and not plan.questions and not plan.clicks and not plan.advance_id
            and plan.page_kind not in ("confirmation", "captcha", "verify_email_code", "verify_email_link"))


async def _select_combo(page, loc, value: str) -> None:
    """Custom dropdown (ARIA combobox/listbox, e.g. MUI, Workday): open it, pick the option
    from ITS list, and check the choice took."""
    from app.apply.page import best_option, close_dropdown, open_options

    await _robust_click(loc)
    await asyncio.sleep(0.7)
    opts = await open_options(loc)
    idx = best_option(value, opts)
    if idx is None and await loc.evaluate("e => e.tagName === 'INPUT'"):
        # Searchable dropdown: type to filter, then look again.
        await loc.fill(value[:40], timeout=5000)
        await asyncio.sleep(1.2)
        opts = await open_options(loc)
        idx = best_option(value, opts)
    if idx is None:
        await close_dropdown(page, loc)
        raise ApplyError(f"option {value!r} not found (have: {', '.join(opts[:6])})")
    handle = await loc.element_handle(timeout=5000)
    frame = await handle.owner_frame()  # the list is in the same frame as the box
    opt = (frame or page).locator(f'[data-jp-opt="{idx}"]')
    if await opt.first.get_attribute("aria-selected") == "true":
        await close_dropdown(page, loc)  # already chosen; clicking again would un-select it
        return
    root = await handle.evaluate_handle(
        "e => e.closest('.MuiAutocomplete-root, [role=group]') || e.parentElement.parentElement")
    try:
        await opt.first.scroll_into_view_if_needed(timeout=3000)
        await opt.first.click(timeout=5000)
    except Exception:  # noqa: BLE001
        await opt.first.evaluate("e => e.click()")
    await asyncio.sleep(0.6)
    await close_dropdown(page, loc)
    # The box may re-render after picking, so check its container: typed value or chip text.
    shown = await root.evaluate("r => (r.querySelector('input') || {}).value || r.innerText || ''")
    if opts[idx].lower()[:20] not in str(shown).lower():
        raise ApplyError(f"picked {opts[idx]!r} but the box doesn't show it")


async def _perform(page, state: PageState, a: Action, resume: Path, secrets: dict) -> None:
    loc = locator(state, a.field_id, a.option_id).first
    if a.action == "fill":
        await loc.fill(a.value, timeout=10000)
    elif a.action == "select_combo":
        await _select_combo(page, loc, a.value)
    elif a.action in ("account_email", "account_password", "verification_code"):
        value = secrets.get({"account_email": "email", "account_password": "password",
                             "verification_code": "code"}[a.action])
        if not value:
            raise ApplyError(f"no value available for {a.action}")
        await loc.fill(value, timeout=10000)
    elif a.action == "select_option":
        await loc.select_option(label=a.value, timeout=10000)
    elif a.action == "select_radio":
        try:
            await loc.check(timeout=5000)
        except Exception:  # noqa: BLE001  (custom-styled radios hide the input)
            await loc.check(timeout=5000, force=True)
    elif a.action in ("check", "uncheck"):
        fn = loc.check if a.action == "check" else loc.uncheck
        try:
            await fn(timeout=5000)
        except Exception:  # noqa: BLE001
            await fn(timeout=5000, force=True)
    elif a.action == "upload_resume":
        await loc.set_input_files(str(resume), timeout=15000)
    await asyncio.sleep(0.15)


async def _robust_click(loc) -> None:
    """Normal click; then scrolled + forced; then a DOM click. Raises ApplyError if all fail."""
    try:
        await loc.click(timeout=8000)
        return
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
        await loc.click(timeout=5000, force=True)
        return
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        await loc.evaluate("e => e.click()")
    except Exception as e:  # noqa: BLE001
        raise ApplyError(f"couldn't click: {type(e).__name__}") from e


def _auto_upload(page, resume: Path, job_id: int | None = None) -> None:
    """Answer any native "choose a file" dialog with the resume (e.g. "Upload resume" buttons)."""
    async def on_chooser(chooser) -> None:
        try:
            await chooser.set_files(str(resume))
            if job_id is not None:
                _event(job_id, "apply.resume_uploaded", how="file dialog", file=resume.name)
        except Exception as e:  # noqa: BLE001
            log.warning("file chooser: %s", e)

    page.on("filechooser", lambda fc: asyncio.ensure_future(on_chooser(fc)))


# Which file boxes get the resume. Read from the input itself and the text around it.
RESUME_HINT = re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)
NOT_RESUME = re.compile(r"cover\s*letter|transcript|photo|picture|avatar|portfolio|certificat|"
                        r"writing sample|reference|license|id\s*card|additional", re.I)

FILE_INPUTS_JS = r"""() => Array.from(document.querySelectorAll('input[type=file]')).map((el, i) => {
  let ctx = [el.name, el.id, el.accept, el.getAttribute('aria-label') || '', el.title || ''];
  if (el.labels) for (const l of el.labels) ctx.push(l.innerText || '');
  let p = el.parentElement;
  for (let d = 0; p && d < 4; d++, p = p.parentElement) {
    const t = (p.innerText || '').trim();
    if (t) { ctx.push(t.slice(0, 300)); if (t.length > 40) break; }
  }
  return {i, filled: !!(el.files && el.files.length), disabled: el.disabled, ctx: ctx.filter(Boolean)};
})"""


async def _direct_upload(page, job_id: int, resume: Path, done: set[str]) -> int:
    """Put the resume straight into any empty resume file box on the page (every frame),
    with no clicking. Returns how many were attached. Each box is filled at most once
    per page address, so a site that clears the box after reading it isn't looped on."""
    count = 0
    page_text = ""
    try:
        page_text = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 5000)")
    except Exception:  # noqa: BLE001, S110
        pass
    for fi, frame in enumerate(page.frames):
        try:
            inputs = await frame.evaluate(FILE_INPUTS_JS)
        except Exception:  # noqa: BLE001, S112  (frame went away)
            continue
        for inp in inputs:
            key = f"{frame.url.split('#')[0]}|{fi}|{inp['i']}"
            if inp["filled"] or inp["disabled"] or key in done:
                continue
            # The closest text that says what the box is for decides (own label before page text).
            verdict = None
            for piece in inp["ctx"]:
                if NOT_RESUME.search(piece):
                    verdict = False
                    break
                if RESUME_HINT.search(piece):
                    verdict = True
                    break
            # A lone, unlabeled file box on a page that talks about resumes is the resume box.
            is_resume = verdict is True or (verdict is None and len(inputs) == 1
                                             and bool(RESUME_HINT.search(page_text)))
            if not is_resume:
                continue
            done.add(key)
            loc = frame.locator("input[type=file]").nth(inp["i"])
            try:
                await loc.set_input_files(str(resume), timeout=15000)
            except Exception as e:  # noqa: BLE001
                _event(job_id, "apply.resume_upload_failed", error=type(e).__name__)
                continue
            ok = False
            try:
                ok = await loc.evaluate("e => !!(e.files && e.files.length)")
            except Exception:  # noqa: BLE001  (the site may have swapped the box out after reading it)
                ok = True
            _event(job_id, "apply.resume_uploaded", how="file box", file=resume.name, confirmed=ok)
            count += 1
    if count:
        await asyncio.sleep(2.0)
        await _settle(page)  # sites often parse the resume and then change the page
    return count


SUBMIT_WATCH_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const errs = [];
  for (const e of document.querySelectorAll('[role=alert], .Mui-error, .error, [class*=error-message], '
      + '[class*=errorMessage], [aria-invalid=true]')) {
    if (!vis(e)) continue;
    let t = (e.innerText || '').trim();
    if (!t && e.getAttribute('aria-invalid') === 'true') {
      const id = e.getAttribute('aria-labelledby') || e.id;
      const lab = id && document.querySelector(`label[for="${id}"]`);
      t = 'invalid field: ' + ((lab && lab.innerText) || e.getAttribute('aria-label') || e.name || '');
    }
    if (t && !errs.includes(t)) errs.push(t.slice(0, 160));
  }
  const text = (document.body ? document.body.innerText : '').slice(0, 20000);
  return {url: location.href.split('#')[0], text, errors: errs.slice(0, 8)};
}"""


async def _wait_after_submit(page, max_seconds: float = 90) -> tuple[str, list[str]]:
    """After clicking Submit, give the site time: some take a minute with no spinner.
    Returns ("changed" | "errors" | "nothing", error texts)."""
    try:
        before = await page.evaluate(SUBMIT_WATCH_JS)
    except Exception:  # noqa: BLE001
        return "changed", []
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        try:
            now = await page.evaluate(SUBMIT_WATCH_JS)
        except Exception:  # noqa: BLE001, S112  (navigating)
            continue
        if now["url"] != before["url"] or CONFIRM_TEXT.search(now["text"]):
            return "changed", []
        if now["errors"] and now["errors"] != before["errors"]:
            return "errors", now["errors"]
        if abs(len(now["text"]) - len(before["text"])) > 200:
            return "changed", []
    return "nothing", before["errors"]


async def _click(page, context, state: PageState, button_id: str):
    """Click a button; if it opens a new tab, continue there. Returns the active page."""
    before = len(context.pages)
    await _robust_click(locator(state, button_id).first)
    await asyncio.sleep(2.0)
    if len(context.pages) > before:
        page = context.pages[-1]
        await page.bring_to_front()
    await _settle(page)
    return page


async def _plan(job_id: int, job_info: dict, state: PageState, account: dict, hint: str = "",
                shot: str | None = None, prefer_fallback: bool = False) -> CheckedPlan:
    facts = _facts()
    images = []
    if shot:
        try:
            images = [(get_settings().data_dir / shot).read_bytes()]
        except OSError:
            images = []
    res = await run_task("form_mapper", SYSTEM_PROMPT, build_prompt(facts, job_info, state, account, hint),
                         PLAN_SCHEMA, job_id=job_id, images=images, prefer_fallback=prefer_fallback)
    return check_plan(res.output, state, {f["id"] for f in facts})


async def _plan_escalating(job_id: int, job_info: dict, state: PageState, account: dict,
                           shot: str | None, stuck: bool, guidance: str | None) -> CheckedPlan:
    """Plan the page; if that makes no progress, look again with the screenshot, then
    with the stronger fallback model. Owner guidance (plain words) is passed along."""
    hint = f"The owner says: {guidance}" if guidance else ""
    if stuck:
        hint = (hint + "\n" if hint else "") + STUCK_HINT
    plan = await _plan(job_id, job_info, state, account, hint, shot if (stuck or guidance) else None)
    if not _no_progress(plan) and not stuck:
        return plan
    for prefer_fallback in (False, True):
        if not _no_progress(plan) and not stuck:
            break
        _set(f"Step {_state['step']}: looking at the screenshot to get unstuck")
        _event(job_id, "apply.escalate", fallback=prefer_fallback)
        plan = await _plan(job_id, job_info, state, account,
                           (hint + "\n" if hint else "") + STUCK_HINT, shot, prefer_fallback)
        stuck = False
    return plan


async def _fit_gate(page, job_id: int, job_info: dict) -> dict | None:
    """Returns the fit result if the job should be skipped, else None."""
    from app.apply.fit import check_fit

    a = _automation()
    if not a.fit_check:
        return None
    _set("Checking how well this job fits your resume")
    text = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 15000)")
    fit = await check_fit(job_info, text, job_id=job_id)
    _event(job_id, "apply.fit", **fit)
    return fit if fit["score"] < a.min_fit_score else None


async def _run(job_id: int, force: bool = False) -> None:
    from playwright.async_api import async_playwright

    s = get_settings()
    with _db() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise ApplyError("Job not found")
        job_info = {"employer": job.employer, "title": job.title, "location": job.location, "url": job.url}
        prev_status = job.status
    resume = _prepare_resume()
    if not _facts():
        # Empty profile: fill it from the resume first instead of asking for every field.
        from app.profile_import import import_from_resume

        _set("Reading your resume into About Me")
        try:
            imported = await import_from_resume()
            _event(job_id, "profile.imported_from_resume", added=imported["added"])
        except Exception as e:  # noqa: BLE001
            log.warning("resume import failed: %s", e)
    dry_run = _automation().dry_run
    _job_status(job_id, "applying")
    _event(job_id, "apply.started", dry_run=dry_run)
    notify(f"Starting application: {job_info['title'] or 'job'} at {job_info['employer'] or job_info['url']}"
           + (" (dry run: will stop before submitting)" if dry_run else ""))

    submitted = False
    outcome = "stopped"
    pw = await async_playwright().start()
    try:
        # Chrome rejects DevTools requests whose Host header is a name, so use the IP.
        ip = socket.gethostbyname(s.browser_cdp_host)
        browser = await pw.chromium.connect_over_cdp(f"http://{ip}:{s.browser_cdp_port}", timeout=20000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        for extra in context.pages[1:]:
            await extra.close()
        await page.bring_to_front()
        _auto_upload(page, resume, job_id)
        context.on("page", lambda pg: _auto_upload(pg, resume, job_id))
        uploaded_boxes: set[str] = set()
        _set("Opening the job page", step=0)
        await page.goto(job_info["url"], wait_until="domcontentloaded", timeout=60000)
        await _settle(page)
        if not force:
            skip = await _fit_gate(page, job_id, job_info)
            if skip:
                outcome = "skipped_fit"
                why = "; ".join(skip["hard_mismatches"]) or skip["summary"]
                notify(f"Skipped {job_info['title'] or 'a job'} at {job_info['employer'] or 'employer'}: "
                       f"fit {skip['score']}/100 ({skip['verdict']}). {why}\n"
                       "If you want it anyway, tap Apply anyway on the Jobs page.")
                raise _Skip()

        last_fp, same_count = None, 0
        mail_since = datetime.now(UTC)
        sign_in_attempts: dict[str, int] = {}
        click_counts: dict[str, int] = {}
        guidance: str | None = None
        asked_rounds = 0
        submit_tries = 0
        wrote: dict[str, str] = {}  # label -> answer the AI made up (shown at approval)
        for step in range(1, MAX_STEPS + 1):
            _check_stop()
            _state["step"] = step
            if await _direct_upload(page, job_id, resume, uploaded_boxes):
                _set(f"Step {step}: attached your resume")
            await _wait_while_busy(page)  # e.g. "Processing..." after a resume upload
            state = await extract(page)
            await read_combo_options(page, state)
            shot = await _screenshot(page, job_id, step, "page")
            fp = state.fingerprint()
            same_count = same_count + 1 if fp == last_fp else 0
            last_fp = fp

            if submitted and CONFIRM_TEXT.search(state.frames[0].get("text", "") if state.frames else ""):
                outcome = "confirmed"
                break

            site = site_key(page.url)
            with _db() as db:
                acct = get_account(db, site)
                account = {"site": site, "account_exists": acct is not None,
                           "login_email": acct.login_email if acct else s.gmail_address}
            _set(f"Step {step}: reading the page and planning")
            plan = await _plan_escalating(job_id, job_info, state, account, shot, same_count >= 1, guidance)
            guidance = None
            _event(job_id, "apply.page", step=step, page_kind=plan.page_kind, fills=len(plan.actions),
                   questions=len(plan.questions), advance=plan.advance_kind, problems=plan.problems[:10],
                   shot=shot)

            if plan.page_kind == "confirmation" and (submitted or CONFIRM_TEXT.search(
                    state.frames[0].get("text", "") if state.frames else "")):
                outcome = "confirmed" if submitted else "already_applied"
                break
            if plan.page_kind == "captcha":
                guidance = await _takeover(job_id, "this page has a CAPTCHA. Please solve it.", shot)
                await _settle(page)
                continue
            if plan.page_kind in ("application_form", "review"):
                with _db() as db:  # got past the account pages: the account works
                    a2 = get_account(db, site)
                    if a2 and a2.status != "active":
                        a2.status = "active"
                        db.commit()

            secrets: dict[str, str] = {}
            uses_account = any(a.action in ACCOUNT_ACTIONS for a in plan.actions)
            if uses_account:
                if plan.page_kind == "sign_in" and not account["account_exists"]:
                    # No account here yet: don't try to sign in; follow the create-account link.
                    plan.actions = [a for a in plan.actions if a.action not in ACCOUNT_ACTIONS]
                else:
                    if plan.page_kind == "sign_in":
                        sign_in_attempts[site] = sign_in_attempts.get(site, 0) + 1
                        if sign_in_attempts[site] > 3:
                            guidance = await _takeover(job_id, f"signing in to {site} keeps failing.", shot)
                            continue
                    with _db() as db:
                        try:
                            a3 = get_or_create_account(db, site, s.gmail_address, job_id=job_id)
                        except VaultError as e:
                            raise ApplyError(str(e)) from e
                        secrets = {"email": a3.login_email, "password": password_of(a3)}
                        db.commit()
                    mail_since = datetime.now(UTC)  # a verification email may follow
            if plan.page_kind in ("verify_email_code", "verify_email_link"):
                want = "code" if plan.page_kind == "verify_email_code" else "link"
                _set(f"Step {step}: checking Gmail for the verification {want}")
                try:
                    found = await wait_for_verification(site, mail_since, want, should_stop=_check_stop,
                                                        context=context)
                except GmailNotSignedIn:
                    guidance = await _takeover(job_id, "sign in to your job Gmail in JobPilot's browser "
                                    "(open a new tab, go to gmail.com), so I can read the "
                                    "verification email.", shot)
                    continue
                if not found:
                    guidance = await _takeover(job_id, f"no verification email from {site} arrived. "
                                    "Check Gmail (and Spam).", shot)
                    continue
                _event(job_id, "apply.verification", kind=want, site=site)
                if want == "link":
                    await page.bring_to_front()
                    await page.goto(found, wait_until="domcontentloaded", timeout=60000)
                    await _settle(page)
                    continue
                secrets["code"] = found

            if plan.clicks:
                clicked = False
                for bid in plan.clicks:
                    key = f"{page.url.split('#')[0]}|{state.all_buttons()[bid]['text']}"
                    click_counts[key] = click_counts.get(key, 0) + 1
                    if click_counts[key] <= 4:  # e.g. "Add" once per work-history entry
                        try:
                            await _robust_click(locator(state, bid).first)
                            clicked = True
                        except ApplyError as e:
                            _event(job_id, "apply.click_failed", step=step, error=str(e))
                        await asyncio.sleep(0.8)
                if clicked:
                    continue  # re-read the page with the newly revealed fields
            if same_count >= 3:
                guidance = await _takeover(job_id, "this page isn't changing no matter what I try.", shot)
                same_count = 0
                continue

            _set(f"Step {step}: filling {len(plan.actions)} field(s)")
            failures = []
            for a in plan.actions:
                _check_stop()
                try:
                    await _perform(page, state, a, resume, secrets)
                except Exception as e:  # noqa: BLE001
                    failures.append(f"{a.label[:60]}: {type(e).__name__} {str(e)[:120]}")
            new_written = {a.label[:200]: a.value for a in plan.actions
                           if a.written and f"{a.label[:60]}:" not in " ".join(failures)}
            if new_written:
                wrote.update(new_written)
                _event(job_id, "apply.wrote", step=step, answers=new_written)
            if plan.actions:
                with _db() as db:  # count fact usage
                    for fid in {i for a in plan.actions for i in a.fact_ids}:
                        f = db.get(Fact, fid)
                        if f:
                            f.used_count += 1
                    db.commit()
            if failures:
                _event(job_id, "apply.fill_failures", step=step, failures=failures[:10])
                # Tell the AI next time, so it tries something different instead of repeating.
                guidance = ((guidance + "\n") if guidance else "") + (
                    "These fields could NOT be filled on the last try: " + "; ".join(failures[:8])
                    + ". Check their options again and pick an exact option, or try another way.")

            if plan.questions:  # fill what we can first (above), then ask for the rest in one message
                asked_rounds += 1
                if asked_rounds > 6:
                    guidance = await _takeover(job_id, "I keep needing answers on this page and can't "
                                               "fill it.", shot)
                    continue
                _set(f"Step {step}: asking you on Telegram ({len(plan.questions)} question(s))")
                lines = [f"{job_info['employer'] or 'This application'} needs a few things I don't know yet:"
                         if len(plan.questions) > 1 else
                         f"{job_info['employer'] or 'This application'} needs something I don't know yet:"]
                for i, q in enumerate(plan.questions, 1):
                    extra = f" (options: {' / '.join(q.options)})" if q.options else ""
                    lines.append(f"{i}. {q.question}{extra}")
                lines.append("\nReply in one message, however you like. I'll remember it for next time.")
                ans = await _ask(job_id, "\n".join(lines), [], kind="info")
                if ans is None:
                    raise Stopped("No answer from owner in time")
                continue  # re-plan this page with the new facts

            if plan.advance_kind == "submit":
                full = await _screenshot(page, job_id, step, "ready-to-submit", full=True)
                if dry_run:
                    outcome = "dry_run"
                    notify("Dry run finished: the form is filled and I stopped before submitting. "
                           "Check the browser, or turn off Dry run in Settings to submit for real.", full)
                    break
                title = f"{job_info['title'] or 'Job'} at {job_info['employer'] or 'employer'}"
                made_up = ""
                if wrote:
                    made_up = "\n\nI wrote these answers myself:\n" + "\n".join(
                        f"- {k[:80]}: {v[:150]}" for k, v in list(wrote.items())[:6])
                if _automation().auto_submit:
                    _set(f"Step {step}: submitting")
                    if not submit_tries:
                        notify(f"Submitting: {title}.{made_up}", full)
                else:
                    _set(f"Step {step}: waiting for your approval to submit")
                    ans = await _ask(job_id, f"Ready to submit: {title}.{made_up}\n\n"
                                     "Look over the screenshot. Submit?",
                                     ["Submit", "Cancel"], kind="approval", photo=full)
                    if ans != "Submit":
                        raise Stopped("You chose not to submit" if ans else "No approval in time")
                _check_stop()
                submit_tries += 1
                _event(job_id, "apply.submit_clicked", step=step, attempt=submit_tries)
                try:
                    if submit_tries == 2:  # the plain click did nothing last time: try a DOM click
                        await locator(state, plan.advance_id).first.evaluate("e => e.click()")
                    else:
                        page = await _click(page, context, state, plan.advance_id)
                except ApplyError as e:
                    _event(job_id, "apply.click_failed", step=step, error=str(e))
                    continue
                submitted = True
                _set(f"Step {step}: waiting for the site to accept the application")
                result, errors = await _wait_after_submit(page)
                _event(job_id, "apply.submit_result", step=step, result=result, errors=errors)
                if result == "errors" or (result == "nothing" and errors):
                    guidance = ("After clicking Submit the site showed these errors: " + "; ".join(errors)
                                + ". Fix those fields, then submit again.")
                elif result == "nothing" and submit_tries >= 3:
                    guidance = await _takeover(
                        job_id, "I clicked Submit 3 times and the site didn't respond or show an error.",
                        shot)
                continue
            if plan.advance_kind in ("apply", "next") and plan.advance_id:
                _set(f"Step {step}: going to the next page")
                try:
                    page = await _click(page, context, state, plan.advance_id)
                except ApplyError as e:  # re-plan this page (with the screenshot) instead of quitting
                    _event(job_id, "apply.click_failed", step=step, error=str(e))
                continue
            if not plan.actions:
                if await _wait_while_busy(page):
                    continue  # it was still loading: look again instead of asking
                guidance = await _takeover(job_id, "I don't know how to continue from this page.", shot)
        else:
            outcome = "too_many_steps"

    except _Skip:
        pass
    except Stopped as e:
        outcome = "stopped"
        _event(job_id, "apply.stopped", reason=str(e))
        notify(f"Application stopped: {e}")
    except (AIUnavailable, ApplyError) as e:
        outcome = "error"
        _event(job_id, "apply.error", error=str(e)[:500])
        notify(f"Application failed: {str(e)[:300]}")
    except Exception as e:  # noqa: BLE001
        outcome = "error"
        log.exception("apply crashed")
        _event(job_id, "apply.error", error=f"{type(e).__name__}: {str(e)[:300]}")
        notify(f"Application failed: {type(e).__name__}")
    finally:
        try:
            if outcome in ("confirmed", "too_many_steps", "stopped", "error") and submitted:
                last = await _screenshot(page, job_id, 99, "after-submit", full=True)  # noqa: F821
            else:
                last = None
        except Exception:  # noqa: BLE001
            last = None
        await pw.stop()  # disconnect only; the browser keeps running for you
        with _db() as db:  # decisions for this run can't be answered after it ends
            for q in db.scalars(select(OwnerQuestion).where(
                    OwnerQuestion.job_id == job_id, OwnerQuestion.status == "open",
                    OwnerQuestion.kind != "fact")).all():
                q.status = "expired"
            db.commit()

    if outcome == "confirmed":
        _job_status(job_id, "submitted_confirmed")
        notify(f"Submitted and confirmed: {job_info['title']} at {job_info['employer']}", last)
    elif submitted:
        _job_status(job_id, "submitted_uncertain")
        notify("I clicked Submit but didn't see a confirmation. Please check the browser and "
               "set the job status yourself.", last)
    elif outcome == "skipped_fit":
        _job_status(job_id, "skipped")
    elif outcome == "already_applied":
        _job_status(job_id, "submitted_uncertain")
    else:
        restore = prev_status if prev_status not in ("applying", "waiting_for_owner") else "interested"
        _job_status(job_id, restore)
    _event(job_id, "apply.finished", outcome=outcome, submitted=submitted, evidence=last)


async def _guarded(job_id: int, force: bool) -> None:
    try:
        await _run(job_id, force)
    except Exception:  # noqa: BLE001
        log.exception("apply run failed")
    finally:
        _state.update(running=False, message="Idle", step=0)


async def _runner_ok() -> bool:
    import httpx

    try:
        transport = httpx.AsyncHTTPTransport(uds=str(get_settings().ai_socket))
        async with httpx.AsyncClient(transport=transport, timeout=5) as c:
            return (await c.get("http://ai/v1/health")).json().get("ok") is True
    except Exception:  # noqa: BLE001
        return False


async def start(job_id: int, force: bool = False) -> None:
    global _task
    if _state["running"]:
        raise ApplyError("Another application is already running")
    if _automation().paused:
        raise ApplyError("Automation is paused. Unpause it on the Overview page first.")
    with _db() as db:
        if db.get(Job, job_id) is None:
            raise ApplyError("Job not found")
        if db.scalar(select(Resume).where(Resume.is_active.is_(True))) is None:
            raise ApplyError("No active resume. Upload one on the Resume page first.")
    if not await _runner_ok():
        raise ApplyError("The AI runner isn't running on the mini PC (scripts/install-ai-runner.sh).")
    _stop.clear()
    _state.update(running=True, job_id=job_id, step=0, message="Starting", started_at=utcnow().isoformat())
    _task = asyncio.create_task(_guarded(job_id, force))


def stop() -> None:
    _stop.set()


def recover_interrupted() -> None:
    """On startup, jobs left 'applying' by a crash/restart go back to 'interested'."""
    with _db() as db:
        for j in db.scalars(select(Job).where(Job.status.in_(["applying", "waiting_for_owner"]))).all():
            log_event(db, "system", "apply.interrupted", job_id=j.id, old=j.status)
            j.status = "interested"
        for q in db.scalars(select(OwnerQuestion).where(
                OwnerQuestion.status == "open", OwnerQuestion.kind != "fact")).all():
            q.status = "expired"
        db.commit()
