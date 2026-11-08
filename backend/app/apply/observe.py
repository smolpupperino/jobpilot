"""What the browser agent sees, and whether anything meaningful changed.

Three views of the page, each cheap enough to take often:

- UI state (STATE_JS): url, title, headings, active step, open dialogs/menus, field values,
  validation errors, the focused element. No body text, so clocks, ads and animations
  don't count as change. This drives progress detection and per-action verification.
- Playwright's AI accessibility snapshot (`aria_snapshot(mode="ai")`): roles, names,
  states and `[ref=eN]` handles. Refs are valid for that snapshot only; the tool layer
  refuses refs that aren't in the latest one. Compacted before it goes to the model.
- Screenshots in CSS pixels (viewport or a crop around one control), with the mapping
  from image pixels back to page coordinates kept next to them for click_at.

The existing DOM extractor (page.extract) is still the source of field IDs/labels for the
planner; this module adds to it, it doesn't replace it.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.apply.page import SHOWN_JS

STATE_JS = r"""() => {
  const T = (s, n = 100) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const fields = [];
  let i = 0;
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (type !== 'file' && !vis(el)) continue;
    let v;
    if (type === 'checkbox' || type === 'radio') v = el.checked ? '1' : '0';
    else if (el.tagName === 'SELECT') v = el.selectedIndex >= 0 ? T(el.options[el.selectedIndex].text, 60) : '';
    else if (type === 'password') v = el.value ? '*' : '';
    else if (type === 'file') v = el.files && el.files.length ? 'file' : '';
    else v = T(el.value, 80);
    const key = el.dataset.jpId || el.name || el.id || (type + (i++));
    fields.push([key, v, el.getAttribute('aria-invalid') === 'true' ? 1 : 0]);
  }
  for (const el of document.querySelectorAll('[role=combobox]:not(input), [aria-haspopup=listbox]:not(input)')) {
    if (!vis(el)) continue;
    fields.push([el.dataset.jpId || el.id || ('cb' + (i++)), T(el.innerText, 80), 0]);
  }
  // Chosen values shown as chips/single-value boxes next to autocomplete inputs.
  for (const el of document.querySelectorAll('[class*=single-value], [class*=singleValue], [class*=multi-value__label], '
      + '.MuiChip-label, [data-automation-id=selectedItem], .select2-selection__rendered, .chosen-single, [data-jp-chosen]')) {
    if (vis(el)) fields.push(['chip' + (i++), T(el.innerText, 60), 0]);
  }
  const names = els => [...els].filter(vis).map(e => T(e.getAttribute('aria-label')
    || (e.querySelector('h1,h2,h3,legend') || {}).innerText || e.innerText, 60)).slice(0, 5);
  const errors = [...document.querySelectorAll('[role=alert], .error, .errors, [class*=error-message], '
      + '[class*=errorMessage], .Mui-error, [aria-live=assertive]')]
    .filter(vis).map(e => T(e.innerText, 140)).filter(Boolean);
  const a = document.activeElement;
  const focus = a && a !== document.body && a !== document.documentElement ? {
    tag: a.tagName.toLowerCase(), type: (a.getAttribute('type') || '').toLowerCase(),
    role: a.getAttribute('role') || '', expanded: a.getAttribute('aria-expanded') || '',
    autocomplete: a.getAttribute('aria-autocomplete') || '',
    in_popup: !!a.closest('[role=listbox], [role=menu], [role=dialog], [role=tree], [role=grid]'),
    name: T(a.getAttribute('aria-label') || a.getAttribute('placeholder') || a.innerText || a.name || '', 60),
  } : null;
  const clock = /\b\d{1,2}:\d{2}(:\d{2})?\b/g;   // countdowns/clocks in headings aren't progress
  return {
    url: location.href, title: T(document.title, 120),
    headings: [...document.querySelectorAll('h1, h2, h3, legend')].filter(vis)
      .map(h => T(h.innerText, 80).replace(clock, '')).filter(Boolean).slice(0, 12),
    step: [...document.querySelectorAll('[aria-current=step], [aria-current=page], [aria-current=true], '
        + '[role=tab][aria-selected=true]')].filter(vis).map(e => T(e.innerText, 60)).slice(0, 3),
    dialogs: names(document.querySelectorAll('[role=dialog], [role=alertdialog], dialog[open], [aria-modal=true]')),
    expanded: names(document.querySelectorAll('[aria-expanded=true]')),
    options: [...document.querySelectorAll('[role=option], [role=menuitem], [role=treeitem]')].filter(vis)
      .map(o => T(o.innerText, 60)).slice(0, 40),
    errors: [...new Set(errors)].slice(0, 10),
    // Highlighted (not necessarily chosen) rows: aria-selected/active-descendant/hover classes.
    highlighted: [...document.querySelectorAll('[role=option][aria-selected=true], [role=option].hl, '
        + '[class*=highlight], [class*=focused][class*=option], [class*=option--is-focused], .select2-results__option--highlighted')]
      .filter(vis).map(o => T(o.innerText, 60)).slice(0, 3),
    fields, focus,
    busy: !!document.querySelector('[aria-busy=true]'),
  };
}"""


async def ui_state(page, timeout: float = 15) -> dict[str, Any]:
    """UI state of the main frame, with child frames' fields/errors/dialogs merged in."""
    out: dict[str, Any] | None = None
    for i, frame in enumerate(page.frames):  # frames[0] is the main frame
        try:
            st = await asyncio.wait_for(frame.evaluate(STATE_JS), timeout)
        except Exception:  # noqa: BLE001, S112  (detached / navigating frame)
            continue
        if out is None:
            out = st
            continue
        for k in ("errors", "dialogs", "expanded", "options"):
            out[k] = out.get(k, []) + st.get(k, [])
        out["fields"] = out.get("fields", []) + [[f"f{i}:{x[0]}", *x[1:]] for x in st.get("fields", [])]
        if not out.get("focus") and st.get("focus"):
            out["focus"] = st["focus"]
    return out or {"url": getattr(page, "url", ""), "title": "", "headings": [], "step": [], "dialogs": [],
                   "expanded": [], "options": [], "errors": [], "fields": [], "focus": None, "busy": False}


def _h(x: Any) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()[:16]


def page_signature(st: dict) -> str:
    """Where we are: address (without #), title, headings, active step, open dialogs, and which
    fields exist. Changes on navigation, step changes without a URL change, dialogs, new sections."""
    return _h([str(st.get("url", "")).split("#")[0], st.get("title"), st.get("headings"), st.get("step"),
               st.get("dialogs"), sorted(f[0] for f in st.get("fields", []) if not str(f[0]).startswith("chip"))])


def value_signature(st: dict) -> str:
    """What's entered: field values, chosen chips, invalid flags, error messages."""
    return _h([sorted(map(list, st.get("fields", []))), sorted(st.get("errors", []))])


@dataclass
class Change:
    navigated: bool = False
    step_changed: bool = False
    dialog_opened: list[str] = field(default_factory=list)
    dialog_closed: list[str] = field(default_factory=list)
    fields_added: int = 0
    fields_removed: int = 0
    values_changed: list[str] = field(default_factory=list)
    errors_added: list[str] = field(default_factory=list)
    errors_removed: list[str] = field(default_factory=list)
    expanded_changed: bool = False
    options_changed: bool = False
    focus_changed: bool = False
    highlight_changed: bool = False

    @property
    def page_level(self) -> bool:
        return bool(self.navigated or self.step_changed or self.dialog_opened or self.dialog_closed
                    or self.fields_added or self.fields_removed)

    @property
    def meaningful(self) -> bool:
        """Progress: page-level change, or entered values / validation errors changed."""
        return bool(self.page_level or self.values_changed or self.errors_added or self.errors_removed)

    @property
    def any(self) -> bool:
        """Anything a click could have caused, including just opening a menu or moving focus."""
        return (self.meaningful or self.expanded_changed or self.options_changed or self.focus_changed
                or self.highlight_changed)

    def describe(self) -> list[str]:
        out = []
        if self.navigated:
            out.append("page address changed")
        if self.step_changed:
            out.append("the application step/heading changed")
        if self.dialog_opened:
            out.append("dialog opened: " + ", ".join(self.dialog_opened)[:120])
        if self.dialog_closed:
            out.append("dialog closed")
        if self.fields_added:
            out.append(f"{self.fields_added} new field(s) appeared")
        if self.fields_removed:
            out.append(f"{self.fields_removed} field(s) disappeared")
        if self.values_changed:
            out.append(f"{len(self.values_changed)} value(s) changed")
        if self.errors_added:
            out.append("new error: " + "; ".join(self.errors_added)[:200])
        if self.errors_removed:
            out.append("error cleared")
        if self.expanded_changed:
            out.append("a menu/dropdown opened or closed")
        if self.options_changed:
            out.append("the visible options changed")
        if self.highlight_changed:
            out.append("the highlighted option changed (highlighted is not chosen)")
        if self.focus_changed:
            out.append("focus moved")
        return out


def compare(before: dict, after: dict) -> Change:
    c = Change()
    c.navigated = str(before.get("url", "")).split("#")[0] != str(after.get("url", "")).split("#")[0]
    c.step_changed = (before.get("step") != after.get("step") or before.get("headings") != after.get("headings")
                      or before.get("title") != after.get("title"))
    bd, ad = before.get("dialogs") or [], after.get("dialogs") or []
    c.dialog_opened = [d for d in ad if d not in bd]
    c.dialog_closed = [d for d in bd if d not in ad]
    bf = {f[0]: f for f in before.get("fields", []) if not str(f[0]).startswith("chip")}
    af = {f[0]: f for f in after.get("fields", []) if not str(f[0]).startswith("chip")}
    c.fields_added = len(set(af) - set(bf))
    c.fields_removed = len(set(bf) - set(af))
    c.values_changed = [k for k in af if k in bf and list(af[k]) != list(bf[k])]
    bchips = sorted(f[1] for f in before.get("fields", []) if str(f[0]).startswith("chip"))
    achips = sorted(f[1] for f in after.get("fields", []) if str(f[0]).startswith("chip"))
    if bchips != achips:
        c.values_changed.append("chosen value")
    be, ae = before.get("errors") or [], after.get("errors") or []
    c.errors_added = [e for e in ae if e not in be]
    c.errors_removed = [e for e in be if e not in ae]
    c.expanded_changed = (before.get("expanded") or []) != (after.get("expanded") or [])
    c.options_changed = (before.get("options") or []) != (after.get("options") or [])
    c.focus_changed = (before.get("focus") or {}) != (after.get("focus") or {})
    c.highlight_changed = (before.get("highlighted") or []) != (after.get("highlighted") or [])
    return c


class ProgressTracker:
    """Page-level and value-level progress between application steps.

    A step "made progress" when the page moved on (address, step, dialog, sections) OR the
    entered values/errors changed. Filling a field on an otherwise unchanged page is progress;
    a ticking clock or rotating banner is not (body text isn't looked at)."""

    def __init__(self) -> None:
        self.last: dict | None = None
        self.stalled = 0  # consecutive observations with no meaningful change

    def update(self, st: dict) -> Change | None:
        prev, self.last = self.last, st
        if prev is None:
            self.stalled = 0
            return None
        ch = compare(prev, st)
        self.stalled = 0 if ch.meaningful else self.stalled + 1
        return ch


# ---- Accessibility snapshot compaction ----

_LINE = re.compile(r"^(?P<indent>\s*)- (?P<body>.*)$")
_REF = re.compile(r"\[ref=([a-z0-9]+)\]")
_BOX = re.compile(r"\s*\[box=(-?[\d.]+),(-?[\d.]+),([\d.]+),([\d.]+)\]")
_NODE = re.compile(r'^(?P<role>[a-z]+)(?: "(?P<name>(?:[^"\\]|\\.)*)")?(?P<attrs>(?: \[[^\]]*\])*)(?::\s*(?P<text>.*))?$')
_FOCUS_ROLES = ("listbox", "dialog", "alertdialog", "menu", "tree", "grid", "alert")


def parse_refs(snapshot: str) -> dict[str, dict]:
    """ref -> {role, name, text, box} for every element that has a ref."""
    out: dict[str, dict] = {}
    for line in snapshot.splitlines():
        m = _LINE.match(line)
        if not m:
            continue
        body = m.group("body")
        ref = _REF.search(body)
        if not ref:
            continue
        box = _BOX.search(body)
        n = _NODE.match(_BOX.sub("", body))
        out[ref.group(1)] = {
            "role": n.group("role") if n else "",
            "name": (n.group("name") or "") if n else "",
            "text": ((n.group("text") or "") if n else "")[:120],
            "states": re.findall(r"\[(expanded|selected|checked|disabled|active|pressed)[^\]]*\]", body),
            "box": [round(float(x)) for x in box.groups()] if box else None,
        }
    return out


def compact_snapshot(snapshot: str, max_chars: int = 9000, keep_boxes: bool = False) -> str:
    """Make an AI snapshot cheaper without losing what matters: drop unnamed wrapper
    containers (their children move up a level), cap long texts and long option lists, and
    if it's still too long keep popups/dialogs/the focused element and cut the rest."""
    lines = snapshot.splitlines()
    out: list[str] = []
    dropped: list[int] = []  # indents of dropped wrappers
    sibling_options: dict[int, int] = {}
    for line in lines:
        m = _LINE.match(line)
        if not m:  # property lines like "/url: ..." under links
            if not line.strip().startswith("- /"):
                continue
            out.append(line)
            continue
        ind = len(m.group("indent"))
        while dropped and dropped[-1] >= ind:
            dropped.pop()
        body = m.group("body")
        if not keep_boxes:
            body = _BOX.sub("", body)
        bare = _BOX.sub("", _REF.sub("", body)).replace("[cursor=pointer]", "").strip()
        if re.fullmatch(r"(generic|group|none|presentation)\s*(\[active\])?\s*:?", bare):
            if "[active]" not in bare:
                dropped.append(ind)
                continue
        if body.startswith("option") and "[ref=" not in body:
            # native <select> options: the field extractor lists them; keep a few for context
            n = sibling_options.get(ind, 0) + 1
            sibling_options[ind] = n
            if n > 12:
                if n == 13:
                    out.append(" " * max(0, ind - 2 * len(dropped)) + "- (more options...)")
                continue
        else:
            sibling_options.pop(ind, None)
        if len(body) > 220:
            body = body[:217] + "..."
        out.append(" " * max(0, ind - 2 * len(dropped)) + "- " + body)
    text = "\n".join(out)
    if len(text) <= max_chars:
        return text
    # Too long: keep focus regions (popups, dialogs, alerts, the active element) whole, then fill
    # the rest of the budget from the top of the page.
    keep: set[int] = set()
    for i, line in enumerate(out):
        m = _LINE.match(line)
        if not m:
            continue
        body = m.group("body")
        if body.split(" ", 1)[0].rstrip(":") in _FOCUS_ROLES or "[active]" in body or "[expanded]" in body:
            ind = len(m.group("indent"))
            keep.add(i)
            for j in range(i + 1, min(len(out), i + 80)):
                mj = _LINE.match(out[j])
                if mj and len(mj.group("indent")) <= ind:
                    break
                keep.add(j)
    budget = max_chars - sum(len(out[i]) + 1 for i in keep)
    chosen = set(keep)
    for i, line in enumerate(out):
        if i in chosen:
            continue
        if budget - len(line) - 1 < 0:
            break
        chosen.add(i)
        budget -= len(line) + 1
    result, skipped = [], 0
    for i, line in enumerate(out):
        if i in chosen:
            if skipped:
                result.append(f"- ...({skipped} lines not shown; scroll or inspect to see more)")
                skipped = 0
            result.append(line)
        else:
            skipped += 1
    if skipped:
        result.append(f"- ...({skipped} lines not shown; scroll or inspect to see more)")
    return "\n".join(result)[: max_chars + 400]


def subtree(snapshot: str, ref: str) -> str:
    """The lines of `snapshot` for the element with `ref` and everything under it."""
    lines = snapshot.splitlines()
    for i, line in enumerate(lines):
        if f"[ref={ref}]" in line:
            ind = len(line) - len(line.lstrip())
            out = [line[ind:]]
            for nxt in lines[i + 1:]:
                if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= ind:
                    break
                out.append(nxt[ind:])
            return "\n".join(out)
    return ""


async def ai_snapshot(target, timeout_ms: int = 15000, boxes: bool = True) -> str:
    """AI-mode accessibility snapshot of a page or locator; '' if the site can't produce one."""
    try:
        return await target.aria_snapshot(mode="ai", boxes=boxes, timeout=timeout_ms)
    except Exception:  # noqa: BLE001  (old Playwright, frozen page, detached element)
        return ""


# ---- Screenshots with coordinate mapping ----

@dataclass
class Shot:
    """An image the model sees. Image pixel (x, y) -> page viewport CSS pixel:
    (clip_x + x / scale, clip_y + y / scale)."""
    png: bytes
    kind: str  # "viewport" | "crop"
    clip_x: float = 0.0
    clip_y: float = 0.0
    scale: float = 1.0
    width: int = 0
    height: int = 0

    def to_css(self, x: float, y: float) -> tuple[float, float]:
        return self.clip_x + x / self.scale, self.clip_y + y / self.scale

    def contains(self, x: float, y: float) -> bool:
        return 0 <= x <= self.width and 0 <= y <= self.height


def png_size(png: bytes) -> tuple[int, int]:
    return int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")


async def viewport_shot(page) -> Shot | None:
    try:
        png = await page.screenshot(scale="css", timeout=15000)
    except Exception:  # noqa: BLE001
        return None
    w, h = png_size(png)
    vw = await page.evaluate("() => window.innerWidth")
    return Shot(png, "viewport", 0, 0, w / max(vw, 1), w, h)


POPUP_BOXES_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  return [...document.querySelectorAll('[role=listbox], [role=menu], [role=tree], [role=dialog]')]
    .filter(vis).slice(0, 4).map(e => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; });
}"""


async def crop_shot(page, loc, pad: int = 60) -> Shot | None:
    """Screenshot around one control plus any open list/menu, in CSS pixels."""
    try:
        await loc.scroll_into_view_if_needed(timeout=3000)
        box = await loc.bounding_box(timeout=3000)
        if not box:
            return None
        rects = [[box["x"], box["y"], box["width"], box["height"]]]
        rects += await page.evaluate(POPUP_BOXES_JS)
        vw, vh = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
        x0 = max(0, min(r[0] for r in rects) - pad)
        y0 = max(0, min(r[1] for r in rects) - pad)
        x1 = min(vw, max(r[0] + r[2] for r in rects) + pad)
        y1 = min(vh, max(r[1] + r[3] for r in rects) + pad)
        if x1 - x0 < 10 or y1 - y0 < 10:
            return None
        clip = {"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0}
        png = await page.screenshot(clip=clip, scale="css", timeout=15000)
    except Exception:  # noqa: BLE001
        return None
    w, h = png_size(png)
    return Shot(png, "crop", x0, y0, w / max(clip["width"], 1), w, h)


# ---- Observation for the agent ----

@dataclass
class Observation:
    url: str
    title: str
    snapshot: str  # compacted AI snapshot text shown to the model
    refs: dict[str, dict]  # refs valid for THIS observation only
    state: dict
    target_ref: str | None = None
    target_view: str = ""  # the target control's own snapshot line(s)
    target_value: str = ""
    shots: list[Shot] = field(default_factory=list)
    seq: int = 0

    def prompt_view(self) -> str:
        st = self.state
        parts = [f"URL: {self.url[:200]}", f"TITLE: {self.title[:120]}"]
        if st.get("step"):
            parts.append("ACTIVE STEP: " + " | ".join(st["step"]))
        if st.get("dialogs"):
            parts.append("OPEN DIALOGS: " + " | ".join(st["dialogs"]))
        if st.get("expanded"):
            parts.append("EXPANDED (open menus/dropdowns): " + " | ".join(st["expanded"]))
        if st.get("options"):
            parts.append("VISIBLE OPTIONS: " + " | ".join(st["options"][:30]))
        if st.get("errors"):
            parts.append("ERRORS ON PAGE: " + " | ".join(st["errors"]))
        f = st.get("focus")
        if f:
            parts.append("FOCUSED: " + json.dumps(f, ensure_ascii=False))
        if self.target_ref or self.target_view:
            parts.append(f"TARGET CONTROL: ref={self.target_ref or '?'} {self.target_view[:300]}")
            parts.append(f"TARGET CURRENTLY SHOWS: {self.target_value[:200]!r}")
        for i, s in enumerate(self.shots):
            parts.append(f"IMAGE {i}: {s.kind} screenshot, {s.width}x{s.height} px")
        parts.append("ACCESSIBILITY SNAPSHOT (refs valid for this observation only):\n" + self.snapshot)
        return "\n".join(parts)


SCOPE_JS = r"""e => {
  // The form-field group around a control: walk up until the box has the question text.
  let p = e;
  for (let i = 0; i < 6 && p.parentElement; i++) {
    p = p.parentElement;
    const t = (p.innerText || '').trim();
    if (p.matches('fieldset, [role=group], [data-automation-id^=formField], .form-group, .field, li')
        || (t.length > 15 && p.querySelectorAll('input, select, textarea, [role=combobox], button').length >= 1
            && t.length < 1500)) break;
  }
  document.querySelectorAll('[data-jp-scope]').forEach(x => x.removeAttribute('data-jp-scope'));
  p.setAttribute('data-jp-scope', '1');
  return true;
}"""

POPUPS_JS = r"""() => {
  // Open popups anywhere in the document, ARIA or not: role-marked popups, common widget classes,
  // floating layers attached to <body> (portals), and the floating layer holding the focus
  // (a search box that opened inside a menu).
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 20 && r.height > 14 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const floating = e => { const p = getComputedStyle(e).position; return p === 'absolute' || p === 'fixed'; };
  document.querySelectorAll('[data-jp-popup]').forEach(x => x.removeAttribute('data-jp-popup'));
  const cands = [...document.querySelectorAll('[role=listbox], [role=menu], [role=tree], [role=dialog], '
    + '[role=alertdialog], [aria-modal=true], [class*=dropdown-menu], [class*=menu-list], [class*=__menu], '
    + '[class*=popover], [class*=results], [class*=suggest], [class*=autocomplete], .select2-dropdown, '
    + '.chosen-drop, .menu, [class*=Menu], [class*=listbox], [class*=options]')];
  for (const e of document.body ? document.body.children : []) if (floating(e)) cands.push(e);
  let a = document.activeElement;
  for (let i = 0; a && a !== document.body && i < 8; i++, a = a.parentElement) {
    if (floating(a)) { cands.push(a); break; }
  }
  const scope = document.querySelector('[data-jp-scope]');
  let n = 0;
  for (const e of cands) {
    if (n >= 3 || !vis(e) || e.closest('[data-jp-popup]') || (scope && scope.contains(e) && !floating(e))) continue;
    if (scope && e.contains(scope)) continue;   // a container of the whole field, not a popup
    if (!e.querySelector('li, [role=option], [role=menuitem], button, a, input, div, span')) continue;
    if ((e.innerText || '').length > 6000) continue;  // a page section, not a popup
    e.setAttribute('data-jp-popup', String(n++));
  }
  return n;
}"""


async def observe(page, target=None, screenshot: bool = False, budget: int = 9000, seq: int = 0) -> Observation:
    """One observation. With a target locator: the target's own node, its form-field group, and
    any open popups (cheap, focused). Without: the whole page, compacted."""
    st = await ui_state(page)
    snap, target_ref, target_view, target_value = "", None, "", ""
    if target is not None:
        own = await ai_snapshot(target, boxes=True)
        refs_own = _REF.findall(own)
        target_ref = refs_own[0] if refs_own else None
        target_view = _BOX.sub("", own.splitlines()[0] if own else "")
        frame = page.main_frame
        pieces: list[tuple[str, str]] = []  # (heading, snapshot piece) taken from region snapshots
        try:
            handle = await target.element_handle(timeout=3000)
            frame = (await handle.owner_frame()) or page.main_frame
            await target.evaluate(SCOPE_JS)
            scope = await ai_snapshot(frame.locator("[data-jp-scope='1']").first, boxes=True)
        except Exception:  # noqa: BLE001
            scope = own
        pieces.append(("# around the target control:", scope or own))
        for fr in dict.fromkeys([frame, page.main_frame]):  # popups/portals: the target's document, then the top
            try:
                n = await fr.evaluate(POPUPS_JS)
            except Exception:  # noqa: BLE001, S112
                continue
            for i in range(n):
                pop = await ai_snapshot(fr.locator(f"[data-jp-popup='{i}']").first, boxes=True)
                if pop:
                    pieces.append(("# open popup:", pop))
        # Playwright resolves aria-ref against the LAST snapshot of each frame, so take the whole
        # page last and cut the regions out of it: every ref shown then resolves. (Refs are cached
        # per element, so the region snapshots and the full one use the same numbers.)
        full = await ai_snapshot(page, boxes=True)
        out_pieces = []
        for head, piece in pieces:
            first = _REF.search(piece)
            region = subtree(full, first.group(1)) if (first and full) else ""
            out_pieces.append(head + "\n" + compact_snapshot(region or piece,
                                                             budget // 2 if "target" in head else budget // 3,
                                                             keep_boxes=True))
        pieces = out_pieces  # type: ignore[assignment]
        snap = "\n".join(pieces)
        try:
            target_value = str(await target.evaluate(SHOWN_JS))[:300]
        except Exception:  # noqa: BLE001, S110
            pass
    else:
        full = await ai_snapshot(page, boxes=True)
        snap = compact_snapshot(full, budget, keep_boxes=True) if full else "(no accessibility snapshot available)"
    shots: list[Shot] = []
    if screenshot:
        if target is not None:
            c = await crop_shot(page, target)
            if c:
                shots.append(c)
        v = await viewport_shot(page)
        if v:
            shots.append(v)
    return Observation(url=st.get("url", ""), title=st.get("title", ""), snapshot=snap, refs=parse_refs(snap),
                       state=st, target_ref=target_ref, target_view=target_view, target_value=target_value,
                       shots=shots, seq=seq)


# Visible text of the control's field group, without labels and open lists (chips, a chosen value
# shown beside a search box).
GROUP_SHOWN_JS = r"""e => {
  let g = e.closest('[data-jp-scope]');
  if (!g) { g = e; for (let i = 0; i < 3 && g.parentElement; i++) g = g.parentElement; }
  // Open lists are never "what's chosen": skip ARIA lists and any floating layer (absolute/fixed),
  // since dropdown menus float over the page while chips and chosen values don't.
  const floatCache = new Map();
  const floats = n => { for (let p = n; p && p !== g.parentElement; p = p.parentElement) {
      if (p === e) return false;
      if (!floatCache.has(p)) { const pos = getComputedStyle(p).position;
        // a floating layer with several lines is a menu; a single floating value label isn't
        floatCache.set(p, (pos === 'absolute' || pos === 'fixed') && (p.innerText || '').trim().split('\n').length >= 2); }
      if (floatCache.get(p)) return true; } return false; };
  const skip = n => n.closest('label, legend, [role=listbox], [role=option], [role=menu], [data-jp-popup], '
    + '[aria-hidden=true]') || floats(n);
  const out = [];
  const w = document.createTreeWalker(g, NodeFilter.SHOW_TEXT);
  for (let t = w.nextNode(); t; t = w.nextNode()) {
    const p = t.parentElement; if (!p || skip(p)) continue;
    const r = p.getBoundingClientRect(); if (!r.width || !r.height) continue;
    const s = t.textContent.trim(); if (s) out.push(s);
  }
  return out.join(' | ').slice(0, 400);
}"""


# The site's own complaint about a field: aria-invalid, or a visible error message in its field group
# (checked after the field loses focus, when most sites validate). '' = no complaint.
FIELD_ERROR_JS = r"""async e => {
  if (e.ownerDocument.activeElement === e && e.blur) { e.blur(); await new Promise(r => setTimeout(r, 400)); }
  const vis = x => { const r = x.getBoundingClientRect(); const s = getComputedStyle(x);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const g = e.closest('.form-group, .field, fieldset, [class*=form-field], [class*=FormField], [class*=question]')
    || (e.parentElement && e.parentElement.parentElement) || e.parentElement;
  const msgs = g ? [...g.querySelectorAll('[role=alert], .error, .text-danger, .invalid-feedback, .Mui-error, '
      + '.help-block.ng-active, [class*=error-message], [class*=errorMessage], [class*=validation]')]
      .filter(x => vis(x) && x !== e && !x.contains(e)).map(x => (x.innerText || '').trim()).filter(Boolean) : [];
  if (msgs.length) return msgs.join(' ').slice(0, 200);
  return e.getAttribute('aria-invalid') === 'true' ? 'the site marks it invalid' : '';
}"""
