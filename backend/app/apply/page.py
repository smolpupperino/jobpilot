"""Read a page's form (fields, buttons, context) and act on it with Playwright.

The extractor tags each element with a data-jp-id attribute, so the plan can refer
to fields by ID and the code can find them again exactly. The AI never gets to
run code or pick CSS selectors; it only picks IDs from this list.
"""

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

# Bot traps: hidden fields humans never fill. Filling one gets the application flagged,
# so they are dropped before the AI ever sees them.
HONEYPOT = re.compile(r"(for robots|robots only|do not (fill|enter|complete)|leave (this )?(field )?(blank|empty)"
                      r"|honeypot|if you('| a)re (a )?human)", re.I)

EXTRACT_JS = r"""
() => {
  const T = (s, n = 300) => (s || '').replace(/\s+/g, ' ').trim().slice(0, n);
  window.__jpN = window.__jpN || 0;
  const tag = el => { if (!el.dataset.jpId) el.dataset.jpId = 'e' + (++window.__jpN); return el.dataset.jpId; };
  const visible = el => {
    if (el.type === 'file') return true;           // file inputs are often visually hidden
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelOf = el => {
    let t = '';
    if (el.labels && el.labels.length) t = [...el.labels].map(l => l.innerText).join(' ');
    if (!t && el.getAttribute('aria-labelledby'))
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) t = el.getAttribute('placeholder') || '';
    if (!t) t = el.getAttribute('title') || '';
    if (!t) { let p = el.parentElement; for (let i = 0; i < 3 && p && !T(t); i++) { t = p.innerText; p = p.parentElement; } }
    t = T(String(t).replace(/\s*Error\s*[-:][\s\S]*$/, ''));  // site error text isn't part of the label
    // Part of a split field ("When can you start?" -> Year / Month / Day): add the question.
    if (t.length < 3 || GENERIC.test(t) || /^current value is\b/i.test(t)) {
      // The question is in the surrounding fieldset/form field, else in the text above.
      const box = el.closest('fieldset') || el.closest('[data-automation-id^=formField]');
      const ql = box && box.querySelector('legend, label');
      const part = (t.match(/\b(month|day|year|hour|minute)\b/i) || [t])[0];
      const q = ql && ql !== el.labels?.[0] ? T(String(ql.innerText).replace(/\s*Error\s*[-:][\s\S]*$/, '').replace(/\*\s*$/, ''))
        : questionAbove(el);
      if (q) t = T(q + ' - ' + part);
    }
    return t;
  };
  // The question a dropdown answers. Workday-style buttons are labeled with their current value
  // ("Select One Required", "Yes"); the real question is in the surrounding fieldset/form field.
  const questionOf = (el, current) => {
    let lab = labelOf(el);
    const norm = x => T(x).toLowerCase().replace(/\brequired\b|\*/g, '').trim();
    if (!lab || norm(lab) === norm(current) || /^(select( one)?|choose( one)?|please select)\b/i.test(lab)) {
      const box = el.closest('fieldset') || el.closest('[data-automation-id^=formField]');
      const q = box && box.querySelector('legend, label');
      const text = q ? q.innerText : questionAbove(el);
      if (text) lab = T(String(text).replace(/\s*Error\s*[-:][\s\S]*$/, '').replace(/\*\s*$/, ''), 300);
    }
    return lab;
  };
  // Multi-select dropdowns show picked values as chips/tags next to an empty text box.
  const chipsOf = el => {
    const wd = el.closest('[data-automation-id=multiselectInputContainer]');
    const root = (wd && wd.parentElement)
      || el.closest('.MuiAutocomplete-root, [class*=select__control], [class*=multiselect], [class*=Select-root]')
      || (el.parentElement && el.parentElement.parentElement);
    if (!root) return '';
    return T([...root.querySelectorAll('.MuiChip-label, [class*=multi-value__label], [class*=chip], [class*=tag-label], '
      + '[data-automation-id=selectedItem], [class*=single-value], [class*=singleValue]')]
      .map(c => c.innerText).filter(Boolean).join(', '), 200);
  };
  const GENERIC = /^(?!.*name)(year|month|day|date|yyyy|mm|dd|hour|minute|am\/pm|from|to|start|end|first|middle|last)\b.{0,10}$/i;
  const questionAbove = el => {
    let p = el.parentElement;
    for (let i = 0; i < 8 && p; i++, p = p.parentElement) {
      for (const c of p.children) {
        if (c.contains(el) || c.querySelector('input,select,textarea,[role=combobox]')) continue;
        const q = T(c.innerText || '', 200);
        if (q.length > 3 && !/^\(?required\)?$/i.test(q) && !GENERIC.test(q)) return q;
      }
    }
    return '';
  };
  const groupLabel = el => {
    const fs = el.closest('fieldset'); if (fs) { const lg = fs.querySelector('legend'); if (lg) return T(lg.innerText); }
    const rg = el.closest('[role=radiogroup],[role=group]');
    if (rg) return T(rg.getAttribute('aria-label') || (document.getElementById(rg.getAttribute('aria-labelledby') || '') || {}).innerText || '');
    let p = el.parentElement; for (let i = 0; i < 4 && p; i++) { const t = T(p.innerText, 400); if (t.length > 20) return t; p = p.parentElement; }
    return '';
  };
  const fields = [], seenRadio = {};
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.tagName === 'INPUT' ? (el.type || 'text') : el.tagName.toLowerCase());
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type)) continue;
    if (!visible(el) || el.disabled) continue;
    // Invisible helper boxes (e.g. react-select's hidden "required" input): not real fields.
    if (type !== 'file' && el.getAttribute('aria-hidden') === 'true' && el.tabIndex < 0) continue;
    if (type !== 'file' && type !== 'checkbox' && type !== 'radio' && getComputedStyle(el).opacity === '0'
        && el.tabIndex < 0) continue;
    const required = !!(el.required || el.getAttribute('aria-required') === 'true');
    if (type === 'radio') {
      const key = el.name || tag(el);
      if (!seenRadio[key]) {
        seenRadio[key] = { id: tag(el), kind: 'radio', label: groupLabel(el), required, options: [], value: '' };
        fields.push(seenRadio[key]);
      }
      let opt = labelOf(el) || el.value;
      // A radio choice is its own short label ("No"), not "<question> - No".
      const cut = opt.lastIndexOf(' - ');
      if (cut > 15 && opt.length - cut - 3 <= 40) opt = opt.slice(cut + 3);
      seenRadio[key].options.push({ id: tag(el), label: T(opt, 150) });
      if (el.checked) seenRadio[key].value = T(opt, 150);
      if (required) seenRadio[key].required = true;
      continue;
    }
    const f = { id: tag(el), kind: type, label: labelOf(el), required, value: '' };
    // Search-and-pick boxes are dropdowns too, even without ARIA roles: Workday's "selectinput"
    // ("0 items selected"), and other multiselect search inputs. Typing alone doesn't choose anything.
    const pickBox = el.getAttribute('data-uxi-widget-type') === 'selectinput'
      || !!el.closest('[data-automation-id=multiselectInputContainer]');
    if (el.tagName === 'INPUT' && (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox'
        || pickBox) && !['checkbox', 'radio', 'file', 'password'].includes(type)) {
      f.kind = 'combobox'; f.options = []; f.value = T(el.value, 200) || chipsOf(el); fields.push(f); continue;
    }
    if (type === 'checkbox') {
      f.value = el.checked ? 'checked' : 'unchecked';
      // One box of a "pick one/several" group ("What is your desired salary?" -> "$90,000 - $100,000"):
      // put the group's question in front so the AI knows what it's answering.
      const fs = el.closest('fieldset'), lg = fs && fs.querySelector('legend');
      const q = lg ? T(lg.innerText, 200) : '';
      if (q && fs.querySelectorAll('input[type=checkbox]').length > 1) {
        f.group = q;
        if (!f.label.includes(q)) f.label = T(q + ' - ' + f.label);
      }
    }
    else if (type === 'file') { f.accept = el.accept || ''; f.value = el.files && el.files.length ? el.files[0].name : ''; }
    else if (type === 'password') f.value = el.value ? '(filled)' : '';
    else if (el.tagName === 'SELECT') {
      f.kind = 'select';
      f.options = [...el.options].filter(o => !o.disabled).slice(0, 250).map(o => T(o.text, 150));
      f.value = el.selectedIndex >= 0 ? T(el.options[el.selectedIndex].text, 150) : '';
    } else f.value = T(el.value, 500);
    if (el.maxLength > 0) f.maxlength = el.maxLength;
    fields.push(f);
  }
  // Custom dropdowns built from buttons/divs (Workday, React-select, ...).
  const comboEls = new Set();
  for (const el of document.querySelectorAll('[role=combobox], [aria-haspopup=listbox]')) {
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) || !visible(el)) continue;
    if (el.closest('[role=combobox]') !== el && el.closest('[role=combobox]')) continue;
    // Site header/navigation menus (account Settings, language) aren't form fields.
    if (el.closest('header, nav, [role=banner], [role=navigation], [data-automation-id=utilityButtonBar]')) continue;
    comboEls.add(el);
    const current = T(el.innerText || el.getAttribute('aria-valuetext') || '', 150);
    const aria = el.getAttribute('aria-label') || '';
    fields.push({ id: tag(el), kind: 'combobox', label: questionOf(el, current) || T(aria),
                  required: el.getAttribute('aria-required') === 'true' || /\brequired\b/i.test(aria),
                  options: [], value: /^select( one)?$/i.test(current) ? '' : current });
  }
  const buttons = [];
  for (const el of document.querySelectorAll('button, input[type=submit], input[type=button], a, [role=button]')) {
    if (!visible(el) || el.disabled || comboEls.has(el)) continue;
    const text = T(el.innerText || el.value || el.getAttribute('aria-label') || '', 80);
    if (!text) continue;
    // Links: keep ones that act as buttons (href "#", "#0", "javascript:", or a click handler)
    // and ones whose words matter for applying (incl. "Don't have an account yet?").
    // Plain navigation links (Privacy, About us, ...) are left out.
    if (el.tagName === 'A') {
      const href = (el.getAttribute('href') || '').trim();
      const jsLink = !href || /^#/.test(href) || /^javascript:/i.test(href)
        || el.hasAttribute('onclick') || el.hasAttribute('ng-click') || el.hasAttribute('data-ng-click');
      const useful = /apply|continue|next|submit|start|begin|review|save|proceed|account|register|sign ?up|sign ?in|log ?in|create|new user|join|guest|upload|add |agree|accept|confirm/i.test(text);
      if (!useful && !(jsLink && !/privacy|cookie|terms|help|faq|language|skip to/i.test(text))) continue;
    }
    buttons.push({ id: tag(el), text, type: (el.getAttribute('type') || el.tagName).toLowerCase() });
    if (buttons.length >= 60) break;
  }
  // Other things a person could click: upload labels ("Browse"), tabs, menu items, elements with
  // click handlers. Kept cheap on huge pages (Workday): explicit markers only, textContent (no
  // layout), and a time budget.
  const listed = new Set(buttons.map(b => b.id));
  const t0 = performance.now();
  const extras = document.querySelectorAll('label, [role=link], [role=tab], [role=menuitem], [onclick], [ng-click]');
  for (let n = 0; n < extras.length && n < 1500; n++) {
    if (buttons.length >= 90 || performance.now() - t0 > 250) break;
    const el = extras[n];
    if (el.tagName === 'LABEL' && !(el.control && el.control.type === 'file')) continue;  // field labels are fields
    if (el.closest('button, a, [role=button], [role=combobox], [role=listbox], select')) continue;
    const text = T(el.textContent, 80);
    if (!text || text.length > 60 || !visible(el) || comboEls.has(el)) continue;
    const id = tag(el);
    if (listed.has(id)) continue;
    listed.add(id);
    buttons.push({ id, text, type: 'clickable' });
  }
  const errors = [...document.querySelectorAll('[role=alert], .error, .errors, [aria-invalid=true]')]
    .map(e => T(e.innerText || e.getAttribute('aria-label') || '', 200)).filter(Boolean).slice(0, 15);
  const headings = [...document.querySelectorAll('h1, h2, h3')].map(h => T(h.innerText, 150)).filter(Boolean).slice(0, 12);
  return {
    url: location.href, title: T(document.title, 200), headings, errors,
    text: T(document.body ? document.body.innerText : '', 3000),
    fields, buttons,
  };
}
"""


@dataclass
class PageState:
    frames: list[dict[str, Any]] = field(default_factory=list)  # one extract result per frame

    def all_fields(self) -> dict[str, dict]:
        out = {}
        for i, fr in enumerate(self.frames):
            for f in fr["fields"]:
                out[f"{i}:{f['id']}"] = f
        return out

    def all_buttons(self) -> dict[str, dict]:
        out = {}
        for i, fr in enumerate(self.frames):
            for b in fr["buttons"]:
                out[f"{i}:{b['id']}"] = b
        return out

    def fingerprint(self) -> str:
        main = self.frames[0] if self.frames else {}
        sig = [main.get("url", "").split("#")[0], main.get("title", "")]
        for _fid, f in sorted(self.all_fields().items()):
            sig.append(f"{f['kind']}|{f['label'][:60]}")
        return hashlib.sha256(json.dumps(sig).encode()).hexdigest()[:16]

    def for_prompt(self) -> dict:
        """Compact view for the AI: frame-qualified IDs, no internal noise."""
        fields, buttons = [], []
        for i, fr in enumerate(self.frames):
            for f in fr["fields"]:
                if HONEYPOT.search(f.get("label") or ""):
                    continue
                g = {k: v for k, v in f.items() if k not in ("id",)}
                g["id"] = f"{i}:{f['id']}"
                if f["kind"] == "radio":
                    g["options"] = [o["label"] for o in f["options"]]
                fields.append(g)
            for b in fr["buttons"]:
                buttons.append({"id": f"{i}:{b['id']}", "text": b["text"]})
        main = self.frames[0] if self.frames else {}
        return {
            "url": main.get("url"), "title": main.get("title"), "headings": main.get("headings"),
            "errors": [e for fr in self.frames for e in fr.get("errors", [])][:15],
            "page_text_excerpt": main.get("text", "")[:2500],
            "fields": fields[:150], "buttons": buttons[:60],
        }


class PageUnresponsive(Exception):
    """The page stopped answering (e.g. its script is stuck in a loop)."""


async def extract(page, timeout: float = 45) -> PageState:
    """Run the extractor in the main frame and any child frames that have fields.
    Never waits forever: a page that doesn't answer within `timeout` raises PageUnresponsive."""
    frames = []
    for frame in page.frames:
        try:
            data = await asyncio.wait_for(frame.evaluate(EXTRACT_JS), timeout)
        except TimeoutError as e:
            if frame == page.main_frame:
                raise PageUnresponsive("the page stopped responding") from e
            continue
        except Exception:  # noqa: BLE001, S112  (detached or cross-origin-blocked frames)
            continue
        if frame == page.main_frame or data["fields"] or data["buttons"]:
            data["_frame"] = frame
            frames.append(data)
    # Keep the main frame first.
    frames.sort(key=lambda d: d["_frame"] != page.main_frame)
    st = PageState(frames=frames)
    return st


# Options of an OPEN custom dropdown: only the list this dropdown controls (aria-controls /
# aria-owns), else visible listboxes. Only visible options; native <select> <option>s elsewhere
# on the page don't count. Each option is tagged data-jp-opt=<index> so it can be clicked.
OPEN_OPTIONS_JS = r"""(el) => {
  const doc = el.ownerDocument;
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  let boxes = [];
  for (const a of ['aria-controls', 'aria-owns']) {
    const id = el.getAttribute(a); const b = id && doc.getElementById(id); if (b) boxes.push(b);
  }
  if (!boxes.length) boxes = [...doc.querySelectorAll('[role=listbox]')].filter(vis);
  if (!boxes.length) boxes = [doc];
  doc.querySelectorAll('[data-jp-opt]').forEach(o => o.removeAttribute('data-jp-opt'));
  const out = [];
  for (const b of boxes) {
    for (const o of b.querySelectorAll('[role=option], [data-automation-id=promptOption]')) {
      if (o.tagName === 'OPTION' || !vis(o)) continue;
      // Already-chosen "pills" of other boxes aren't choices in this list.
      if (o.closest('[data-automation-id=selectedItemList], [data-automation-id=selectedItem]')) continue;
      const t = (o.innerText || o.textContent || '').trim().replace(/\s+/g, ' ');
      if (!t || out.includes(t.slice(0, 150))) continue;
      o.setAttribute('data-jp-opt', String(out.length)); out.push(t.slice(0, 150));
    }
  }
  return out;
}"""


# Names for the same country (lists say "United States of America", answers say "USA").
_US = {"united states", "united states of america", "usa", "us", "u.s.", "u.s.a.", "america", "the united states"}


def country_alias(value: str, options: list[str]) -> int | None:
    """If `value` names the United States, the option that does (not "...Minor Outlying Islands")."""
    v = " ".join(value.lower().replace("(+1)", "").split()).strip(" .")
    if v not in _US:
        return None
    for i, o in enumerate(options):
        if " ".join(o.lower().split()).strip(" .") in _US:
            return i
    return None


def best_option(value: str, options: list[str]) -> int | None:
    """Index of the option meaning `value`: exact, then case-insensitive, then a unique
    prefix/contains match ("No" -> "No, I will not need sponsorship")."""
    v = " ".join(value.split()).lower()
    opts = [o.lower() for o in options]
    if not v:
        return None
    if v in opts:
        return opts.index(v)
    alias = country_alias(value, options)
    if alias is not None:
        return alias
    for test in (lambda o: o.startswith(v), lambda o: v in o, lambda o: o in v and len(o) > 1):
        hits = [i for i, o in enumerate(opts) if test(o)]
        if len(hits) == 1:
            return hits[0]
    return None


async def close_dropdown(page, loc) -> None:
    """Close an open dropdown. Escape only while it's open: on some sites Escape on the page
    itself means "cancel the application?"."""
    try:
        if await loc.evaluate("e => e.getAttribute('aria-expanded') === 'true'"):
            await page.keyboard.press("Escape")
    except Exception:  # noqa: BLE001, S110  (the box re-rendered: nothing open to close)
        pass


# Placeholder rows some dropdowns show instead of options ("No Items.", "No matches").
NO_MATCH = re.compile(r"^(no (items|matches|results|options|records|data)( found| available)?|nothing found"
                      r"|searching|loading|type to search)\W*$", re.I)


async def open_options(loc) -> list[str]:
    """Real options of an open dropdown (placeholder rows dropped)."""
    return [o for o in await loc.evaluate(OPEN_OPTIONS_JS) if not NO_MATCH.match(o)]


async def read_combo_options(page, state: PageState, limit: int = 12) -> None:
    """Custom dropdowns only show their options when opened: open each, read, close."""
    done = 0
    for qid, f in state.all_fields().items():
        if f["kind"] != "combobox" or f.get("options") or done >= limit:
            continue
        done += 1
        loc = locator(state, qid).first
        try:
            await loc.click(timeout=4000)
            await asyncio.sleep(0.6)
            f["options"] = (await open_options(loc))[:100]
            await close_dropdown(page, loc)
            await asyncio.sleep(0.2)
        except Exception:  # noqa: BLE001, S112  (best effort; planner copes with no options)
            continue


def locator(page_state: PageState, qualified_id: str, option_id: str | None = None):
    idx, el_id = qualified_id.split(":", 1)
    frame = page_state.frames[int(idx)]["_frame"]
    return frame.locator(f'[data-jp-id="{option_id or el_id}"]')


# What the box shows as chosen, ignoring any open option list (its text isn't a choice).
SHOWN_JS = r"""e => {
  // Button-style dropdowns show their choice on the button itself; nearby text (other questions'
  // answers) must not count.
  if (e.tagName !== 'INPUT') return (e.innerText || '') + ' | ' + (e.getAttribute('aria-label') || '');
  const textOf = n => { if (!n) return ''; const c = n.cloneNode(true);
    c.querySelectorAll('[role=listbox], [role=option], [data-automation-id=promptOption], [role=menu]')
      .forEach(x => x.remove());
    return c.textContent || ''; };
  const wd = e.closest('[data-automation-id=multiselectInputContainer]');
  const box = (wd && wd.parentElement) || e.closest('.MuiAutocomplete-root, [role=group]')
    || e.parentElement.parentElement;
  const inputs = box ? [...box.querySelectorAll('input:not([type=hidden])')].map(i => i.value) : [];
  return [e.value || '', ...inputs, textOf(box)].join(' | ');
}"""


# A control the page reader missed, found next to its visible label (worker._find_control).
FIND_CONTROL_JS = r"""(label) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase().replace(/\s*\*+$/, '');
  const want = norm(label);
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  let best = null;
  for (const el of document.querySelectorAll('label, legend, span, div, p, h3, h4, h5, strong, dt, th')) {
    const own = norm([...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' '));
    if (!own || !(own === want || (own.startsWith(want) && want.length > 3) || (want.startsWith(own) && own.length > 3)))
      continue;
    if (vis(el)) { best = el; break; }
  }
  if (!best) return false;
  document.querySelectorAll('[data-jp-widget]').forEach(x => x.removeAttribute('data-jp-widget'));
  if (best.control) { best.control.setAttribute('data-jp-widget', '1'); return true; }
  let box = best.parentElement;
  for (let i = 0; i < 3 && box; i++, box = box.parentElement) {
    const c = [...box.querySelectorAll('input:not([type=hidden]), select, textarea, [role=combobox], [tabindex], '
        + '[aria-haspopup], button, [class*=select], [class*=dropdown], [class*=control]')]
      .find(x => x !== best && !best.contains(x) && vis(x) && (best.compareDocumentPosition(x) & 4));
    if (c) { c.setAttribute('data-jp-widget', '1'); return true; }
  }
  return false;
}"""
