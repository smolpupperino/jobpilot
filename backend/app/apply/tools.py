"""JobPilot's browser tools for the AI agent: a fixed, validated action set.

The model asks for an action ({"type": "click", "ref": "e12"}); this module decides whether
it's allowed, runs it with Playwright, checks what actually changed on the page, and returns
structured feedback. The model never runs code, never picks CSS selectors, and can only
target refs from the latest observation, field IDs from the page reader, or screenshot
points that are checked before clicking.

Safety lives here, not in the prompt:
- Final submit (Submit, Finish, Send application, "Apply" on a filled form) is never pressed
  by the agent; the worker does that after its own checks.
- Account-wrecking, cancel/withdraw and social sign-in buttons are refused (planner.NEVER_CLICK).
- Enter is only pressed inside dropdowns/search boxes (Enter in a plain field submits forms).
- In "field" mode the agent may only type text derived from the value the planner already
  validated against the owner's facts, and may only pick options that match it. In "page"
  mode (getting unstuck) it can't type at all.
- Marketing opt-ins are never checked; password fields are never typed into.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.apply.observe import Observation, compare, ui_state
from app.apply.page import OPEN_OPTIONS_JS, best_option, country_alias
from app.apply.planner import APPLY_TEXT, MARKETING_LABEL, NEVER_CLICK, SUBMIT_TEXT, is_agreement

ACTIONS = ["click", "click_at", "fill", "type_text", "press_key", "select_option", "check", "uncheck",
           "scroll", "focus", "inspect", "inspect_options", "wait", "upload_resume"]
KEYS = {"Enter", "Tab", "Shift+Tab", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Space",
        "Backspace", "Home", "End", "PageDown", "PageUp"}


class Cat(StrEnum):
    NOT_FOUND = "element_not_found"
    DETACHED = "element_detached"
    OBSCURED = "element_obscured"
    DISABLED = "element_disabled"
    NO_EFFECT = "click_had_no_effect"
    DROPDOWN_CLOSED = "dropdown_did_not_open"
    OPTION_UNAVAILABLE = "option_unavailable"
    NOT_PERSISTED = "selection_did_not_persist"
    INPUT_REJECTED = "field_did_not_accept_input"
    VALIDATION = "validation_rejected_value"
    MODAL = "unexpected_modal"
    NAV_TIMEOUT = "navigation_timeout"
    UNRESPONSIVE = "page_unresponsive"
    AUTH = "authentication_required"
    CAPTCHA = "captcha_required"
    SITE_ERROR = "site_server_error"
    DUPLICATE = "possible_duplicate_submission"
    REFUSED = "refused_by_safety_rules"
    BAD_REQUEST = "invalid_request"
    UNKNOWN = "unknown_failure"


@dataclass(frozen=True)
class Recovery:
    strategy: str  # retry_after_wait | reobserve | other_method | fallback_model | takeover | stop
    hint: str


# What to do about each kind of failure. "hint" is shown to the model with the failure.
RECOVERY: dict[Cat, Recovery] = {
    Cat.NOT_FOUND: Recovery("reobserve", "That element isn't there now. Use a ref from the NEW snapshot, scroll, "
                                         "or open the section that contains it."),
    Cat.DETACHED: Recovery("reobserve", "The page re-rendered and that ref is stale. Pick the element again from "
                                        "the new snapshot."),
    Cat.OBSCURED: Recovery("other_method", "Something covers it (popup, banner, sticky bar). Close the overlay "
                                           "(its close/No thanks button or Escape), scroll it into view, or use "
                                           "click with method \"dom\"."),
    Cat.DISABLED: Recovery("other_method", "It's disabled. Usually another required field or choice must be done "
                                           "first; check errors and empty required fields."),
    Cat.NO_EFFECT: Recovery("other_method", "Nothing changed. Try a different way: click its arrow/icon or label, "
                                            "method \"keyboard\" (focus + Enter/Space), ArrowDown on the focused "
                                            "control, or click_at on the screenshot."),
    Cat.DROPDOWN_CLOSED: Recovery("other_method", "The list didn't open. Try clicking the arrow icon, focusing it "
                                                  "and pressing ArrowDown or Space, or typing into it."),
    Cat.OPTION_UNAVAILABLE: Recovery("other_method", "The wanted option isn't listed. Type a shorter search (first "
                                                     "word), scroll the list, or wait for results. If the real "
                                                     "options don't include it, finish with status impossible."),
    Cat.NOT_PERSISTED: Recovery("other_method", "Highlighting or typing isn't choosing. Click the option itself, or "
                                                "press Enter while it is highlighted, then check the field shows it."),
    Cat.INPUT_REJECTED: Recovery("other_method", "The field didn't keep the text. Try type_text (real key presses), "
                                                 "clear it first, or a different format the field expects."),
    Cat.VALIDATION: Recovery("other_method", "The site rejected the value. Read the error text and enter the same "
                                             "value in the format it asks for."),
    Cat.MODAL: Recovery("other_method", "A popup is in the way. Close it with its No thanks/Close button or Escape "
                                        "(never agree to marketing)."),
    Cat.NAV_TIMEOUT: Recovery("retry_after_wait", "The page is still loading. Wait, then look again."),
    Cat.UNRESPONSIVE: Recovery("stop", "The page stopped responding."),
    Cat.AUTH: Recovery("stop", "The site wants a sign-in; JobPilot handles accounts itself."),
    Cat.CAPTCHA: Recovery("takeover", "A CAPTCHA needs the owner."),
    Cat.SITE_ERROR: Recovery("stop", "The site itself failed (server error)."),
    Cat.DUPLICATE: Recovery("stop", "The application may already be submitted; stop and check."),
    Cat.REFUSED: Recovery("other_method", "Not allowed by JobPilot's rules. Choose a different action."),
    Cat.BAD_REQUEST: Recovery("other_method", "The request was malformed (unknown action, missing ref or value)."),
    Cat.UNKNOWN: Recovery("fallback_model", "Unknown failure. Look at the page again and try another way."),
}


class Refused(Exception):
    pass


class BadRequest(Exception):
    pass


@dataclass
class ActionResult:
    action: str
    target: str = ""
    found: bool = False
    actionable: bool = False
    executed: bool = False
    verified: bool = False
    category: str = ""
    error: str = ""
    changes: list[str] = field(default_factory=list)
    page_changed: bool = False  # refs from the previous observation are no longer safe to use
    reinspect: bool = False
    want_screenshot: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    ms: int = 0

    @property
    def ok(self) -> bool:
        return self.executed and self.verified and not self.category

    def for_model(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action, "target": self.target[:120], "ok": self.ok,
                               "executed": self.executed, "verified": self.verified}
        if self.category:
            out["error_category"] = self.category
            out["error"] = self.error[:300]
            rec = RECOVERY.get(Cat(self.category))
            if rec:
                out["hint"] = rec.hint
        if self.changes:
            out["what_changed"] = self.changes[:6]
        elif self.executed:
            out["what_changed"] = ["nothing visible changed"]
        if self.data:
            out["data"] = self.data
        return out


def classify(e: BaseException) -> tuple[Cat, str]:
    """Playwright exception -> failure category + a short message (first line only)."""
    s = str(e)
    first = s.strip().splitlines()[0][:200] if s.strip() else type(e).__name__
    low = s.lower()
    if "intercepts pointer events" in low or "outside of the viewport" in low:
        return Cat.OBSCURED, first
    if "not enabled" in low or "element is disabled" in low or "is disabled" in low:
        return Cat.DISABLED, first
    if "not attached" in low or "detached" in low or "node is detached" in low:
        return Cat.DETACHED, first
    if "execution context was destroyed" in low or "frame was detached" in low:
        return Cat.DETACHED, "the page navigated or re-rendered during the action"
    if "resolved to 0 elements" in low or "element(s) not found" in low:
        return Cat.NOT_FOUND, first
    if "not visible" in low or "element is not visible" in low:
        return Cat.OBSCURED, first
    if "cannot type" in low or "not an <input>" in low or "is not an input" in low or "not editable" in low:
        return Cat.INPUT_REJECTED, first
    if "did not find some options" in low:
        return Cat.OPTION_UNAVAILABLE, first
    if "navigation" in low and "timeout" in low:
        return Cat.NAV_TIMEOUT, first
    if isinstance(e, TimeoutError) or "timeout" in low:
        return Cat.UNKNOWN, "timed out: " + first
    return Cat.UNKNOWN, first


# ---- element info + safety ----

INFO_FN = r"""(e) => {
  const T = (s, n = 150) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  // A <label> acts for its control (clicking it toggles the checkbox/radio it labels).
  const ctl = (e.tagName === 'LABEL' && e.control) ? e.control : e;
  const tag = e.tagName.toLowerCase(), type = (ctl.getAttribute('type') || '').toLowerCase();
  const role = ctl.getAttribute('role') || e.getAttribute('role') || '';
  let label = '';
  if (ctl.labels && ctl.labels.length) label = [...ctl.labels].map(l => l.innerText).join(' ');
  if (!label && ctl.getAttribute('aria-labelledby'))
    label = ctl.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
  label = T(label || ctl.getAttribute('aria-label') || ctl.getAttribute('placeholder') || ctl.title || '');
  if (!label && (type === 'checkbox' || type === 'radio')) label = T((e.closest('label, li, div') || {}).innerText || '');
  const text = T(tag === 'input' || tag === 'textarea' ? (e.value || '') : (e.innerText || e.textContent || ''));
  const a = e.closest('a[href]');
  const pop = e.closest('[role=listbox], [role=menu], [role=tree], [role=grid], [data-jp-popup], .select2-dropdown, .chosen-drop');
  const form = e.closest('form') || document;
  const filled = [...form.querySelectorAll('input, select, textarea')].filter(x => !['hidden', 'submit', 'button',
    'checkbox', 'radio'].includes((x.type || '').toLowerCase()) && x.value).length;
  return {
    tag, type, role, label, text,
    name: T(e.getAttribute('aria-label') || text || e.value || e.title || ''),
    disabled: !!(ctl.disabled || ctl.getAttribute('aria-disabled') === 'true'),
    editable: (ctl.tagName === 'INPUT' && !['checkbox', 'radio', 'file', 'submit', 'button', 'image', 'reset', 'hidden', 'range',
      'color'].includes(type)) || tag === 'textarea' || e.isContentEditable,
    expanded: e.getAttribute('aria-expanded') || '',
    autocomplete: e.getAttribute('aria-autocomplete') || '',
    href: a ? (a.getAttribute('href') || '') : '',
    abs_href: a ? a.href : '',
    in_popup: !!pop,
    is_option: ['option', 'menuitem', 'treeitem', 'menuitemradio', 'menuitemcheckbox'].includes(role)
      || (!!pop && ['li', 'div', 'span', 'a'].includes(tag) && !e.querySelector('input')),
    checked: !!ctl.checked || ctl.getAttribute('aria-checked') === 'true',
    is_check: type === 'checkbox' || type === 'radio' || ['checkbox', 'radio', 'switch'].includes(role),
    is_select: ctl.tagName === 'SELECT',
    is_file: type === 'file',
    is_password: type === 'password',
    is_iframe: tag === 'iframe',
    in_nav: !!e.closest('header, nav, [role=banner], [role=navigation]'),
    filled,
  };
}"""

POINT_JS = ("([x, y]) => { const hit = document.elementFromPoint(x, y); if (!hit) return null;"
            " const c = hit.closest('button, a, [role=button], [role=option], [role=menuitem], [role=treeitem], "
            "[role=tab], [role=checkbox], [role=radio], [role=combobox], label, input, select, textarea, li, "
            "iframe') || hit; const info = (" + INFO_FN + ")(c); const r = c.getBoundingClientRect();"
            " info.box = [r.x, r.y, r.width, r.height]; return info; }")

FOCUSED_JS = ("() => { const a = document.activeElement;"
              " if (!a || a === document.body || a === document.documentElement || a.tagName === 'IFRAME') return null;"
              " return (" + INFO_FN + ")(a); }")

NEXT_TEXT = re.compile(r"^\s*(next|continue|save (and|&) continue|proceed|next step|go on|ok|okay|close|"
                       r"add|upload|search|browse|done editing)\b", re.I)
FINALISH = re.compile(r"\b(send|complete|finali[sz]e|confirm( and)? (submit|send))\b", re.I)
TEXT_KEYS = ("text", "label", "name")


def desc(info: dict, extra: str = "") -> str:
    """How a target is described to the model and in memory: role "name"."""
    name = str(info.get("label") or info.get("name") or "")[:80].replace('"', "'")
    return f'{info.get("role") or info.get("tag") or "element"} "{name}"{extra}'[:140]


def _norm(v: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@.+-]", " ", (v or "").lower())).strip()


def matches_value(text: str, values: list[str]) -> bool:
    """Does an option/display text mean one of the approved values (or a part of a
    "Category > Choice" path)? Used for both choosing and verifying."""
    t = _norm(text)
    if not t:
        return False
    for v in values:
        for part in [v, *[p.strip() for p in v.split(">")]]:
            pv = _norm(part)
            if not pv:
                continue
            if t == pv or re.search(rf"(^|\s){re.escape(pv)}($|\s)", t):
                return True  # the option says the value, maybe with more words ("Oregon (OR)")
            if len(t) >= 3 and re.search(rf"(^|\s){re.escape(t)}($|\s)", pv) and len(t) >= 0.6 * len(pv):
                return True  # the option is most of the value ("Bachelor of Science" ~ "Bachelor of Science (BS)")
            if country_alias(part, [text]) is not None:
                return True
    return False


@dataclass
class Policy:
    """What this agent run may do. mode "field": operate ONE control toward values the planner
    already validated. mode "page": get unstuck (close popups, open sections, move on), no data entry."""
    mode: str = "page"
    values: list[str] = field(default_factory=list)
    checkbox_ok: bool = False  # field mode on a checkbox the planner decided to check
    allow_upload: bool = True

    def text_ok(self, text: str) -> bool:
        if self.mode != "field":
            return False
        if text == "":
            return True  # clearing a box
        t = _norm(text)
        tdig = re.sub(r"\D", "", text)
        for v in self.values:
            for part in [v, *[p.strip() for p in v.split(">")]]:
                pv = _norm(part)
                if t and t in pv:
                    return True
                vdig = re.sub(r"\D", "", part)
                # same digits in another format (dates, phone numbers)
                if len(tdig) >= 4 and tdig == vdig and len(re.sub(r"[\d\W_]", "", text)) == 0:
                    return True
        return False

    def option_ok(self, text: str) -> bool:
        if self.mode != "field":
            return False
        if not re.search(r"[A-Za-z0-9]", text or ""):
            return True  # icon-only (arrow, clear) inside the popup
        return matches_value(text, self.values)


def click_refusal(info: dict, policy: Policy) -> str | None:
    """Why this click must not happen (None = fine)."""
    text = " ".join(str(info.get(k) or "") for k in TEXT_KEYS)
    if info.get("is_password") or info.get("is_file"):
        return None if info.get("is_file") and policy.allow_upload else "not a clickable control here"
    if NEVER_CLICK.search(text):
        return f"never pressed: {text[:60]!r} (cancel/withdraw/account-wrecking/social sign-in)"
    if info.get("is_check"):
        lab = str(info.get("label") or info.get("text") or "")
        if not info.get("checked") and MARKETING_LABEL.search(lab):
            return "never opt in to marketing"
        if info.get("type") == "radio" or info.get("role") == "radio":
            if not policy.option_ok(lab):
                return "choosing this answer isn't part of the approved value"
        elif not info.get("checked") and not (policy.checkbox_ok or is_agreement(lab)):
            return "checking this box isn't part of the approved plan"
        return None
    if info.get("is_option") or info.get("in_popup"):
        if policy.mode == "field" and not info.get("editable") and not policy.option_ok(str(info.get("text") or "")):
            return (f"option {str(info.get('text'))[:60]!r} doesn't match the approved value; never substitute "
                    "a different option (finish with status impossible if the right one isn't offered)")
        return None
    is_button = info.get("tag") in ("button", "a") or info.get("role") in ("button", "link") \
        or info.get("type") in ("submit", "button", "image")
    if is_button or info.get("type") == "submit":
        if SUBMIT_TEXT.search(text):
            return f"{text[:40]!r} looks like the final submit; JobPilot presses that itself after its checks"
        if APPLY_TEXT.search(text) and int(info.get("filled") or 0) >= 3:
            return "an Apply button on a filled form may submit the application; JobPilot handles that"
        if info.get("type") == "submit" and (FINALISH.search(text) or (policy.mode == "field"
                                                                       and not NEXT_TEXT.search(text))):
            return "a form submit button isn't part of operating this field"
        if policy.mode == "field" and info.get("abs_href") and not re.match(r"^\s*(#|javascript:|$)",
                                                                             str(info.get("href") or "")):
            return "following links isn't part of operating this field"
    return None


# ---- the tool runner ----

class BrowserTools:
    def __init__(self, page, context=None, policy: Policy | None = None, resume: Path | None = None) -> None:
        self.page = page
        self.context = context
        self.policy = policy or Policy()
        self.resume = resume

    # -- targeting --

    def _resolve(self, req: dict, obs: Observation):
        ref = str(req.get("ref") or "").strip()
        if ref:
            if ref not in obs.refs:
                raise BadRequest(f"ref {ref!r} is not in the latest observation (refs expire after every look)")
            return self.page.locator(f"aria-ref={ref}"), ref
        raise BadRequest("this action needs a ref from the snapshot")

    async def _info(self, loc) -> dict:
        if await loc.count() == 0:
            raise _Fail(Cat.DETACHED, "the element is gone (the page changed since the snapshot)")
        return await loc.first.evaluate(INFO_FN, timeout=5000)

    async def _focused(self) -> dict | None:
        for frame in self.page.frames:
            try:
                info = await frame.evaluate(FOCUSED_JS)
            except Exception:  # noqa: BLE001, S112
                continue
            if info:
                return info
        return None

    async def _await_change(self, before: dict, timeout: float = 1.5) -> dict:
        """Wait (event-ish: short polls) until the UI changes or `timeout` passes."""
        deadline = time.monotonic() + timeout
        after = before
        while time.monotonic() < deadline:
            await asyncio.sleep(0.15)
            try:
                after = await ui_state(self.page, timeout=5)
            except Exception:  # noqa: BLE001, S112  (navigating)
                continue
            if compare(before, after).any:
                await asyncio.sleep(0.25)  # let a re-render finish
                try:
                    after = await ui_state(self.page, timeout=5)
                except Exception:  # noqa: BLE001, S110
                    pass
                break
        return after

    # -- entry point --

    async def run(self, req: dict, obs: Observation) -> ActionResult:
        t0 = time.monotonic()
        typ = str(req.get("type") or "")
        res = ActionResult(action=typ)
        pages_before = len(self.context.pages) if self.context is not None else 0
        try:
            before = await ui_state(self.page, timeout=10)
        except Exception:  # noqa: BLE001
            before = obs.state
        verify = None
        try:
            if typ not in ACTIONS:
                raise BadRequest(f"unknown action {typ!r}")
            verify = await getattr(self, f"_a_{typ}")(req, obs, res)
            res.executed = True
        except Refused as e:
            res.category, res.error = Cat.REFUSED, str(e)
        except BadRequest as e:
            res.category, res.error = Cat.BAD_REQUEST, str(e)
        except _Fail as e:
            res.category, res.error = e.cat, e.msg
            res.executed = e.executed
        except Exception as e:  # noqa: BLE001
            cat, msg = classify(e)
            res.category, res.error = cat, msg
        after = before
        if res.executed or res.category in (Cat.DETACHED, Cat.UNKNOWN):
            after = await self._await_change(before, 2.5 if typ in ("click", "click_at", "press_key") else 1.2)
        ch = compare(before, after)
        res.changes = ch.describe()
        res.page_changed = ch.page_level
        if self.context is not None and len(self.context.pages) > pages_before:
            self.page = self.context.pages[-1]
            await self.page.bring_to_front()
            res.changes.append("a new tab opened; continuing in it")
            res.page_changed = True
        if res.executed and not res.category:
            if verify is not None:
                try:
                    v = await verify(ch, after)
                except Exception as e:  # noqa: BLE001
                    v = (False, Cat.UNKNOWN, f"couldn't verify: {classify(e)[1]}")
                if isinstance(v, tuple):
                    res.verified, cat, msg = v
                    if not res.verified:
                        res.category, res.error = cat, msg
                else:
                    res.verified = bool(v)
            else:
                res.verified = True
        if res.page_changed:
            res.reinspect = True
        res.ms = int((time.monotonic() - t0) * 1000)
        return res

    # -- actions --

    async def _a_click(self, req, obs, res):
        loc, ref = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = True
        res.target = desc(info)
        if info.get("disabled"):
            raise _Fail(Cat.DISABLED, "the element is disabled")
        why = click_refusal(info, self.policy)
        if why:
            raise Refused(why)
        res.actionable = True
        method = str(req.get("method") or "normal")
        if method == "dom":
            await loc.first.evaluate("e => e.click()", timeout=5000)
        elif method == "keyboard":
            await loc.first.focus(timeout=5000)
            await self.page.keyboard.press("Enter" if info.get("tag") in ("a", "button") else "Space")
        else:
            await loc.first.click(timeout=5000)
        was_option = bool(info.get("is_option"))

        async def verify(ch, after):
            if was_option:
                if ch.values_changed or ch.page_level or ch.expanded_changed or ch.options_changed:
                    return True  # chosen (menu closed / value changed) or a sub-list opened
                if ch.highlight_changed or after.get("highlighted"):
                    return False, Cat.NOT_PERSISTED, ("the option is only HIGHLIGHTED; the field didn't change, so "
                                                      "it is not chosen. Press Enter while it is highlighted, or "
                                                      "use ArrowDown/Enter on the dropdown")
                return False, Cat.NOT_PERSISTED, "clicked the option but the field didn't change"
            # Focus landing on the clicked element itself isn't an effect; focus jumping to another
            # input (a search box that opened) is.
            foc = after.get("focus") or {}
            moved_elsewhere = ch.focus_changed and foc.get("tag") in ("input", "textarea") \
                and not info.get("editable")
            if ch.meaningful or ch.expanded_changed or ch.options_changed or ch.highlight_changed or moved_elsewhere:
                return True
            if info.get("role") == "combobox" or info.get("expanded"):
                return False, Cat.DROPDOWN_CLOSED, "clicked the dropdown but no list opened"
            return False, Cat.NO_EFFECT, "the click changed nothing"
        return verify

    async def _a_click_at(self, req, obs, res):
        try:
            x, y, img = float(req.get("x")), float(req.get("y")), int(req.get("image") or 0)
        except (TypeError, ValueError) as e:
            raise BadRequest("click_at needs numeric x, y and an image index") from e
        if not (0 <= img < len(obs.shots)):
            raise BadRequest(f"there is no image {img} in the latest observation")
        shot = obs.shots[img]
        if not shot.contains(x, y):
            raise BadRequest(f"({x:.0f},{y:.0f}) is outside image {img} ({shot.width}x{shot.height})")
        cx, cy = shot.to_css(x, y)
        vw, vh = await self.page.evaluate("() => [window.innerWidth, window.innerHeight]")
        if not (0 <= cx < vw and 0 <= cy < vh):
            raise BadRequest("that point is outside the visible page")
        info = await self._point_info(cx, cy)
        if info is None:
            raise _Fail(Cat.NOT_FOUND, "nothing at that point")
        res.found = True
        res.target = desc(info, f" at ({cx:.0f},{cy:.0f})")
        why = click_refusal(info, self.policy)
        if why:
            raise Refused(why)
        if info.get("disabled"):
            raise _Fail(Cat.DISABLED, "the element at that point is disabled")
        res.actionable = True
        await self.page.mouse.click(cx, cy)

        async def verify(ch, after):
            return True if ch.any else (False, Cat.NO_EFFECT, "clicking there changed nothing")
        return verify

    async def _point_info(self, x: float, y: float) -> dict | None:
        """What's under a viewport point, looking inside iframes (incl. cross-origin ones)."""
        frame, fx, fy = self.page.main_frame, x, y
        for _ in range(3):
            info = await frame.evaluate(POINT_JS, [fx, fy])
            if not info or not info.get("is_iframe"):
                return info
            child = None
            for cand in frame.child_frames:
                try:
                    el = await cand.frame_element()
                    b = await el.bounding_box()
                except Exception:  # noqa: BLE001, S112
                    continue
                if b and b["x"] <= x < b["x"] + b["width"] and b["y"] <= y < b["y"] + b["height"]:
                    child, fx, fy = cand, x - b["x"], y - b["y"]
                    x, y = fx, fy
                    break
            if child is None:
                return info
            frame = child
        return None

    async def _text_target(self, req, obs, res):
        loc, ref = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = True
        res.target = desc(info)
        if info.get("is_password"):
            raise Refused("JobPilot fills password fields itself")
        if not info.get("editable"):
            raise _Fail(Cat.INPUT_REJECTED, f"{info.get('tag')} isn't a text box")
        if info.get("disabled"):
            raise _Fail(Cat.DISABLED, "the box is disabled")
        text = str(req.get("text") if req.get("text") is not None else "")
        if len(text) > 2000:
            raise BadRequest("text too long")
        if not self.policy.text_ok(text):
            raise Refused("text must come from the approved value for this field (no other text may be typed)")
        res.actionable = True
        return loc, text

    async def _a_fill(self, req, obs, res):
        loc, text = await self._text_target(req, obs, res)
        await loc.first.fill(text, timeout=5000)

        async def verify(ch, after):
            try:
                now = await loc.first.input_value(timeout=2000)
            except Exception:  # noqa: BLE001  (contenteditable, or replaced after a re-render)
                return True
            if _norm(now) == _norm(text) or (text and _norm(text) in _norm(now)):
                return True
            return False, Cat.INPUT_REJECTED, f"the box shows {now[:60]!r} instead"
        return verify

    async def _a_type_text(self, req, obs, res):
        loc, text = await self._text_target(req, obs, res)
        if req.get("clear", True):
            await loc.first.fill("", timeout=5000)
        await loc.first.press_sequentially(text, delay=45, timeout=15000)

        async def verify(ch, after):
            return True  # typing into a search box legitimately changes what it shows; the goal check decides
        return verify

    async def _a_press_key(self, req, obs, res):
        key = str(req.get("key") or "")
        if key not in KEYS:
            raise BadRequest(f"key must be one of {sorted(KEYS)}")
        if req.get("ref"):
            loc, _ = self._resolve(req, obs)
            info = await self._info(loc)
            await loc.first.focus(timeout=5000)
        foc = await self._focused()
        res.found = True
        res.target = "focused " + desc(foc) if foc else "page"
        if key in ("Enter", "Space"):
            info = foc or {}
            popupish = (info.get("role") in ("combobox", "searchbox", "option", "listbox", "menuitem", "treeitem")
                        or info.get("autocomplete") or info.get("expanded") == "true" or info.get("in_popup")
                        or info.get("type") == "search")
            if key == "Space" and info.get("is_check"):
                why = click_refusal(info, self.policy)
                if why:
                    raise Refused(why)
            elif info.get("tag") in ("button", "a") or info.get("role") in ("button", "link"):
                why = click_refusal(info, self.policy)
                if why:
                    raise Refused(why)
            elif key == "Enter" and not popupish and info.get("tag") != "textarea":
                raise Refused("Enter in a plain form field can submit the form; only use it in dropdowns/search boxes")
        res.actionable = True
        await self.page.keyboard.press(" " if key == "Space" else key)

        async def verify(ch, after):
            return True if ch.any else (False, Cat.NO_EFFECT, f"pressing {key} changed nothing")
        return verify

    async def _a_select_option(self, req, obs, res):
        loc, _ = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = True
        res.target = desc({**info, "role": "select"})
        if not info.get("is_select"):
            raise _Fail(Cat.BAD_REQUEST, "select_option is for native <select> boxes; click custom dropdowns")
        opts = await loc.first.evaluate("s => [...s.options].map(o => o.text.trim())")
        want = str(req.get("option") or "")
        idx = best_option(want, opts)
        if idx is None:
            import difflib

            close = difflib.get_close_matches(want, opts, n=5, cutoff=0.5)
            raise _Fail(Cat.OPTION_UNAVAILABLE, f"{want[:60]!r} isn't an option. Closest: {'; '.join(close) or 'none'}."
                                                f" All {len(opts)} options start: {'; '.join(opts[:25])}")
        if not self.policy.option_ok(opts[idx]):
            raise Refused(f"{opts[idx][:60]!r} doesn't match the approved value")
        res.actionable = True
        await loc.first.select_option(index=idx, timeout=5000)

        async def verify(ch, after):
            shown = await loc.first.evaluate("s => s.selectedIndex >= 0 ? s.options[s.selectedIndex].text : ''")
            return True if shown.strip() == opts[idx] else (False, Cat.NOT_PERSISTED, f"shows {shown!r}")
        return verify

    async def _check(self, req, obs, res, want: bool):
        loc, _ = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = True
        lab = str(info.get("label") or info.get("text") or "")
        res.target = desc({**info, "role": info.get("type") or "checkbox", "label": lab})
        if want and MARKETING_LABEL.search(lab):
            raise Refused("never opt in to marketing")
        if want and not (self.policy.checkbox_ok or is_agreement(lab)) and info.get("type") != "radio":
            raise Refused("checking this box isn't part of the approved plan")
        if want and info.get("type") == "radio" and not self.policy.option_ok(lab):
            raise Refused("that choice doesn't match the approved value")
        res.actionable = True
        fn = loc.first.check if want else loc.first.uncheck
        try:
            await fn(timeout=5000)
        except Exception:  # noqa: BLE001  (styled boxes hide the real input)
            await fn(timeout=5000, force=True)

        async def verify(ch, after):
            now = await loc.first.evaluate("e => !!e.checked || e.getAttribute('aria-checked') === 'true'")
            return True if now == want else (False, Cat.NOT_PERSISTED, "the box didn't change")
        return verify

    async def _a_check(self, req, obs, res):
        return await self._check(req, obs, res, True)

    async def _a_uncheck(self, req, obs, res):
        return await self._check(req, obs, res, False)

    async def _a_scroll(self, req, obs, res):
        direction = str(req.get("direction") or "down")
        amount = max(50, min(int(req.get("amount") or 500), 3000))
        dy = -amount if direction == "up" else amount
        if req.get("ref"):
            loc, _ = self._resolve(req, obs)
            info = await self._info(loc)
            res.target = desc(info)
            if direction == "into_view":
                await loc.first.scroll_into_view_if_needed(timeout=5000)
            else:
                await loc.first.evaluate(
                    "(e, dy) => { let p = e; while (p && !(p.scrollHeight > p.clientHeight + 4 && "
                    "/auto|scroll/.test(getComputedStyle(p).overflowY))) p = p.parentElement; "
                    "(p || document.scrollingElement).scrollBy(0, dy); }", dy)
        else:
            res.target = "page"
            await self.page.mouse.wheel(0, dy)
        res.found = res.actionable = True
        res.reinspect = True
        return None

    async def _a_focus(self, req, obs, res):
        loc, _ = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = res.actionable = True
        res.target = desc(info)
        await loc.first.focus(timeout=5000)
        return None

    async def _a_inspect(self, req, obs, res):
        res.target = "page"
        res.found = res.actionable = True
        res.reinspect = True
        res.want_screenshot = bool(req.get("screenshot", True))
        return None

    async def _a_inspect_options(self, req, obs, res):
        loc, _ = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = True
        res.target = desc(info)
        if info.get("is_select"):
            res.actionable = True
            res.data["options"] = (await loc.first.evaluate("s => [...s.options].map(o => o.text.trim())"))[:60]
            return None
        why = click_refusal({**info, "is_option": False, "in_popup": False}, self.policy)
        if why:
            raise Refused(why)
        res.actionable = True
        if info.get("expanded") != "true":
            await loc.first.click(timeout=5000)
        opts: list[str] = []
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                opts = [o for o in await loc.first.evaluate(OPEN_OPTIONS_JS) if o]
            except Exception:  # noqa: BLE001
                opts = []
            if not opts:
                opts = (await ui_state(self.page, timeout=5)).get("options") or []
            if opts:
                break
        res.data["options"] = opts[:40]
        res.reinspect = True

        async def verify(ch, after):
            if opts:
                return True
            return False, Cat.DROPDOWN_CLOSED, "no options appeared (maybe not open, or the list isn't ARIA: " \
                                               "look at the new snapshot/screenshot)"
        return verify

    async def _a_wait(self, req, obs, res):
        ms = max(200, min(int(req.get("ms") or 2000), 8000))
        text = str(req.get("text") or "")[:80]
        res.target = f"text {text!r}" if text else f"{ms} ms"
        res.found = res.actionable = True
        res.reinspect = True
        if text:
            try:
                await self.page.get_by_text(text).first.wait_for(state="visible", timeout=ms)
            except Exception as e:  # noqa: BLE001
                raise _Fail(Cat.NAV_TIMEOUT, f"{text!r} didn't appear within {ms} ms", executed=True) from e
            return None
        before = await ui_state(self.page, timeout=5)
        deadline = time.monotonic() + ms / 1000
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            if compare(before, await ui_state(self.page, timeout=5)).any:
                break
        return None

    async def _a_upload_resume(self, req, obs, res):
        if not self.policy.allow_upload or self.resume is None:
            raise Refused("no resume upload in this task")
        loc, _ = self._resolve(req, obs)
        info = await self._info(loc)
        res.found = True
        res.target = desc({**info, "role": "file box"})
        from app.apply.worker import NOT_RESUME

        if not info.get("is_file"):
            inner = loc.first.locator("input[type=file]")
            if await inner.count() == 0:
                raise _Fail(Cat.INPUT_REJECTED, "no file box there; click the upload button (the file dialog "
                                                "gets the resume automatically)")
            loc = inner
        if NOT_RESUME.search(str(info.get("label") or "")):
            raise Refused("that file box isn't for a resume")
        res.actionable = True
        await loc.first.set_input_files(str(self.resume), timeout=15000)

        async def verify(ch, after):
            try:
                return bool(await loc.first.evaluate("e => !!(e.files && e.files.length)"))
            except Exception:  # noqa: BLE001  (site swapped the box after reading it)
                return True
        return verify


class _Fail(Exception):
    def __init__(self, cat: Cat, msg: str, executed: bool = False) -> None:
        super().__init__(msg)
        self.cat, self.msg, self.executed = cat, msg, executed
