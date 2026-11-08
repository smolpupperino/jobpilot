# Browser agent (adaptive browser interaction)

Added 2026-11-08. Setting: **Settings -> Automation -> "Smart browser agent"**
(`automation.browser_agent`, **off by default**). With it off, the application worker
behaves exactly as before; every new code path is behind that flag.

## Why

JobPilot often had every answer but couldn't operate the page: a dropdown made of plain
divs, a menu that only commits on Enter, a popup in the way, a button that ignores the
first click. The old loop could only (a) run a page-wide plan through fixed Python helpers,
then (b) re-plan the whole page with a screenshot, then (c) ask the owner. The model never
saw what its last action actually did, so it couldn't try another way.

## The hybrid

```
read page -> plan (AI, checked by check_plan) -> fill everything the fast way -> verify
     |                                                     |
     |   field didn't take / control not in the reader /  v
     |   page not moving           +-----------------------------+
     +---------------------------> | adaptive agent (per control |
                                   | or "get unstuck")           |
                                   | observe -> decide -> act -> |
                                   | verify -> recover (bounded) |
                                   +-----------------------------+
                                                 | verified, or a structured failure
                                                 v
                         back to the fast loop (re-plan with the real options, or ask/takeover)
```

The fast path is unchanged and still does ordinary fields in one AI call per page.
The agent is called only for:

| Trigger (worker.py) | Agent goal | What it may do |
|---|---|---|
| A planned fill/select/check failed, or the value didn't stick (`_verify_fills`) | `operate_field(control, label, value)` with the value check_plan already approved | operate that control: click, type the value or part of it, keys, pick the matching option, scroll, inspect |
| The planner names a question it sees but the page reader didn't list (`other_controls`, validated like fills) | same, the control is found next to its visible label | same |
| The page made no meaningful progress twice, or there is no plan for the page | `unstick(reason)` | close popups, press Next/Continue/Add/Apply-to-start, scroll, wait; **no data entry** |

Each field gets at most 2 agent attempts per run, each page at most 2 unstick attempts,
each attempt at most 8 decisions (`agent.MAX_STEPS`) of at most 3 actions.

## The loop (`app/apply/agent.py`)

1. **Goal check first.** For a field: read the control back (`SHOWN_JS`, selected option,
   checked state, chips/chosen value in the field's own group, never text inside an open
   menu). Met = it shows the value, the menu is closed, and it is still there after the
   field loses focus (an autocomplete that only holds typed text clears it on blur). For a
   page goal: a meaningful change since the start (see progress below).
2. **Observe** (`observe.observe`): UI state + Playwright's AI accessibility snapshot
   (`aria_snapshot(mode="ai", boxes=True)`) + screenshots when useful. For a field: the
   control's own node, its form-field group, and any open popups (ARIA lists, common widget
   classes, floating layers attached to `<body>`, the floating layer holding the focus), plus
   a cropped screenshot. For a page: the whole page, compacted.
3. **Decide**: one stateless AI call (`run_task("form_mapper", ...)`, same provider settings
   and fallback as the planner) with the goal, the approved value, the compact memory and the
   observation. Reply: `{thought, status, actions[1-3]}` (`DECISION_SCHEMA`).
4. **Act** (`tools.BrowserTools.run`): validate, execute, wait for the UI to react
   (short polls, no fixed sleeps), diff the UI state, return structured feedback.
5. **Verify** per action (did the list open? did the value change? was the option only
   highlighted?) and per goal (step 1 on the next round).
6. **Recover**: the failure, its category and a hint go into memory; the next decision sees
   them. The same failed action twice is blocked (the signature is role+name, not the ref,
   so a re-render doesn't reset it). Two failed rounds in a row switch to the fallback model.
   CAPTCHA, site server errors, sign-in walls stop the goal at once.

## Browser actions (`app/apply/tools.py`)

| Action | Notes |
|---|---|
| `click {ref, method}` | `normal`, `dom` (element.click(), for covered elements), `keyboard` (focus + Enter/Space) |
| `click_at {x, y, image}` | pixels of screenshot *n*; mapped to CSS pixels via the image's clip/scale; checks what's under the point (inside iframes too) before clicking |
| `fill {ref, text}` / `type_text {ref, text}` | typing is key by key (search boxes, autocompletes, masked fields) |
| `press_key {key, ref?}` | Enter, Tab, Escape, arrows, Space, ... |
| `select_option {ref, option}` | native `<select>` only |
| `check` / `uncheck`, `focus`, `scroll {ref?, direction}` | scroll finds the nearest scrollable container |
| `inspect_options {ref}` | opens a dropdown and lists its options |
| `inspect`, `wait {text?, ms?}` | wait is condition-based (text appears / UI changes), max 8 s |
| `upload_resume {ref}` | resume file boxes only |

Targets are refs from the **latest** observation only (`aria-ref=eN` locators; refs expire on
every look). A ref from an older look is refused ("not in the latest observation"); a ref
whose element vanished reports `element_detached`. `page.get_by_ref()` doesn't exist in the
installed Playwright 1.63; the `aria-ref=` selector does the same job and reaches into
iframes, including cross-origin ones.

Every result says: found, actionable, executed, verified, what changed ("1 value changed",
"a menu opened", "the highlighted option changed (highlighted is not chosen)", "new error:
...", "a new tab opened"), the error category and a recovery hint.

### Error categories and what happens

`element_not_found`, `element_detached` -> look again, use a fresh ref.
`element_obscured`, `element_disabled`, `click_had_no_effect`, `dropdown_did_not_open`,
`option_unavailable`, `selection_did_not_persist`, `field_did_not_accept_input`,
`validation_rejected_value`, `unexpected_modal` -> the hint suggests another method; the
model picks one. `navigation_timeout` -> wait and look again. `unknown_failure` twice ->
fallback model. `captcha_required` -> owner (the only hand-off). `site_server_error`,
`page_unresponsive`, `authentication_required`, `possible_duplicate_submission` -> stop the
goal; the worker's existing handling takes over. `refused_by_safety_rules` -> choose
something else.

## Safety (enforced in code, whatever the model says)

- The agent never presses the final submit: Submit, Finish, Send/Complete application,
  "Apply" on a filled form, a `type=submit` "Done/Send/Confirm" button, and in field mode any
  form-submit button or navigating link. The worker still does Submit after its own checks.
- `planner.NEVER_CLICK` (cancel, withdraw, delete account, change email/password, social
  sign-in) applies to clicks, keyboard "clicks" and screenshot clicks.
- Enter is refused in plain text fields (it submits forms); allowed in dropdowns/search boxes.
- **Field mode:** the only text that may be typed is the approved value, a part of it (to
  search), or the same digits in another format. Options may only be chosen when they match
  the approved value (whole words; "No" never matches "Not applicable"); otherwise the agent
  must report the real options and the worker sends them back to the planner, which checks
  them against the facts again. No silent substitution.
- **Page mode:** no typing, no choosing answers.
- Marketing opt-ins are never checked; only agreement boxes or the box the plan approved.
- Password fields are never typed into; account fields and codes stay with the old code.
- Page text is untrusted: the prompt says so, and none of the above depends on the model obeying it.
- Events never contain the shared password, emailed codes or security answers (redacted by
  value), and typed text isn't logged in `browser.action_requested`.

### Submission safety

With the flag on, before pressing a submit button a second time the worker runs
`_submit_guard`: confirmation showing -> done; same page and nothing happened, or site errors
-> retry (max 2 silent tries instead of 3); the page moved on to a new step with fields ->
that's a new step; the page changed but shows no confirmation, no errors and nothing to fill
-> **stop as "submitted, not confirmed"** instead of risking a duplicate. A job is still only
"confirmed" on a confirmation page.

## Progress detection (`observe.ProgressTracker`)

The old `PageState.fingerprint()` (URL, title, field kinds/labels) ignored values, so filling a
field looked like no progress. With the flag on, stuck detection uses the UI state:

- **page level:** address (without `#`), title, headings, active step (`aria-current`, selected
  tab), open dialogs, which fields exist (new sections, "Add" results);
- **value level:** field values, checked states, chosen chips/values, invalid flags, error
  messages.

Progress = either changed. Body text isn't used, so clocks, rotating banners and ads don't
count; heading clock patterns (`4:59`) are stripped. Per-action effects also include menus
opening, options changing, highlight and focus moves (not progress, but proof a click did
something).

## Working memory (`agent.WorkingMemory`)

One per application run, owned by JobPilot (model calls stay stateless): objective, stage,
the last 60 attempts (action, target, detail, expected outcome, ok, category, result),
failure counts per action signature, verified field values, navigation, current blocker.
Each prompt gets a summary: objective, verified values, the last 10 attempts with real
results, a "Do NOT repeat" list. The planner also gets the last 6 when the agent has done
something. The fallback model gets the same summary. Fields the agent verified aren't
re-entered just because the page reader misreads a custom widget (re-checked first).

## Events (job history)

`browser.recovery_started`, `browser.observation`, `browser.action_requested` (model,
provider, status, actions without typed text), `browser.action_executed`/`browser.action_failed`
(target, expected, category, error, changes, ms), `browser.action_verified`,
`browser.fallback_model`, `browser.navigation`, `browser.recovery_succeeded`/`browser.blocked`
(steps, actions, failures, AI calls, AI ms, fallback used, duration), `browser.submit_guard`,
`browser.summary` (per run). The Jobs page shows a compact "Browser agent" section under a job's
details (where it got stuck and why); the raw events stay out of the timeline.

## Also changed (helps both paths)

- `check_plan` kept only the category of a nested answer ("Career Websites" instead of
  "Career Websites > Corporate Website") when the options had been read: now it keeps the path.
- The page reader now reads react-select style "single value" boxes, so a chosen value isn't
  seen as empty and re-filled forever.
- `MARKETING_LABEL` now also catches "Text me / Call me about other job openings".

## Tests

- `./scripts/test.sh`: unit tests (progress, compaction, refs, coordinate mapping, value
  grounding, click safety, failure classification, memory/bans, planner `other_controls` and
  nested paths, flag default), then the browser tests.
- `./scripts/test-browser.sh`: real headless Chromium (test image
  `backend/tests/browser/Dockerfile`, Debian's Chromium) against local fixture pages
  (`tests/browser/fixtures.py`, `static/widgets.js`): one configurable dropdown component
  covering ARIA combobox, searchable with portal and no ARIA, React-style async autocomplete,
  Enter-to-choose, ArrowDown+Enter only, portal, nested, multi-select, fails to open, reverts
  once, delayed results, plain divs, never commits; plus modal, Add section, conditional
  field, React re-render, SPA steps, nested scroll container, cross-origin iframe, validation
  errors, obscured button, ignored click, missing button, unresponsive page, delayed content,
  mid-task popup, CAPTCHA, HTTP 500, prompt injection, destructive buttons (by ref and by
  coordinates), unapproved text, secret redaction, duplicate-submit guard, and the whole
  application through the real worker (flag off and on).
- The model in these tests is `tests/browser/policy.py`: a deliberately simple rule-based
  stand-in that reads only the prompt text the real model gets. It's deterministic and free,
  and it fails when the observation or the feedback is missing something, like a model would.
  The browser behavior is real. The benchmark can also run the agent's decisions with the
  owner's real model through the AI runner (`--live`).

## Benchmark

See "Benchmark results" below; rerun with:

```
./scripts/test-browser.sh bench                                   # stand-in model
BENCH_DOCKER_ARGS="-v $PWD/local-run:/run/jobpilot-ai" ./scripts/test-browser.sh bench --live \
  --provider codex --model gpt-6.1-terra --effort low --fallback-provider codex --fallback-model gpt-6-luna
```

`--live` writes the route into the benchmark's own temp database; the owner's settings aren't
touched. In the application part the page planner is still the stand-in (`harness.fake_plan`);
only the agent's decisions use the live model.

## Benchmark results (measured 2026-11-08, fixture site only)

"Previous" = the old executor (`_select_combo`, up to 3 tries, which is what the old loop allowed
before asking the owner; each retry = one planner re-plan, counted as an AI call). "Hybrid" =
one fast try, then the agent. `dd_never_commit` is a broken widget nothing can set: the right
result is a bounded failure with nothing committed, so it counts as "correct", not "completed".
The "previous" column also already benefits from the shared fixes listed above.

### With the owner's model (`--live`: Codex gpt-6.1-terra, effort low, fallback gpt-6-luna)

| Dropdowns (13) | completed | correct | needs owner | AI calls | AI time | failed actions | wall time |
|---|---|---|---|---|---|---|---|
| previous | 8 | 9 | 4 | 13 (re-plans) | n/a | 0 | 52 s |
| hybrid | 12 | 13 | 0 | 23 | 110 s | 5 | 165 s |

Old executor failed on: searchable plain-div portal, Enter-to-choose autocomplete,
ArrowDown+Enter menu, plain-div dropdown. The agent solved each in 1-3 calls. The 7 easy
dropdowns needed no AI call in either mode.

| Stuck pages (10) | completed | AI calls | notes |
|---|---|---|---|
| agent | 10/10 | 13 total | modal, Add section, obscured, ignored click, missing button, delayed, unresponsive, SPA step; CAPTCHA and HTTP 500 stopped with 0 AI calls |

| Whole application (real worker) | outcome | values correct | planner calls | agent calls | takeovers | time |
|---|---|---|---|---|---|---|
| previous (flag off) | stopped: takeover | n/a | 8 | 0 | 1 | 18 s |
| agent on | confirmed | yes (all 8, no marketing) | 5 | 6 | 0 | 72 s |

### With the deterministic stand-in model (`tests/browser/policy.py`)

Dropdowns: previous 8/13 completed, hybrid 12/13 (13/13 correct). Stuck pages: 10/10.
Whole application: previous stopped for a takeover, agent on confirmed with correct values
(5 planner + 5 agent calls). Repeated-action loops: the repeat block fired 1 time in the
stand-in run, 0 in the final live run.

### Found by the live run and fixed

The first live run failed both keyboard-only menus (8 calls each). The model chose correctly
("press Enter on the dropdown"), but the tool reported the ref as gone: Playwright resolves
`aria-ref` only against the most recent snapshot, and the observation took several. Now the
full page is snapshotted last and the regions are cut from it. After the fix: 3 calls each.

Not measured: real ATS sites (by design: no real submissions in tests), and the planner
itself with the live model (the application benchmark keeps the stand-in planner, so only the
agent's decisions are live). Speed: the agent path is slower per hard widget (about 5 s per AI
call) but replaces a Telegram question/takeover.

## Limits (honest list)

- Tested against fixtures modelled on real widgets, not against real ATS sites. The first
  real runs with the flag on are the real test; watch the "Browser agent" section of the job.
- Fixture coverage of real-site widget libraries is by behavior, not by their actual code
  (no React Select/Select2/Chosen/Workday bundles are vendored). The existing fast handlers
  for Select2/Chosen/Workday are unchanged and still run first.
- The agent can't fix missing facts (by design) and can't enter account fields.
- `other_controls` relies on the planner noticing a question the reader missed (from the
  screenshot/page text); if it doesn't, the page is "stuck" and only the unstick mode runs,
  which can't enter data.
- Shadow DOM: Playwright's snapshot and `aria-ref` see open shadow roots; the old page reader
  (and so the planner's field list) still doesn't, so such fields reach the agent only via
  `other_controls`.
- Hard-coded sleeps remain in the old fast path (`_select_combo`, `_settle`); the new code
  uses condition polling.
- Each agent decision is one AI call (about 5 s with the current Codex route); a hard widget
  costs 1-8 calls. Ordinary pages cost nothing extra.
