# Status

Last updated: 2026-11-08

Labels: **Done** = built and tested. **Partial** = some of it works. **Planned** = not started.

## Phase 0: Inventory

| Item | Status | Notes |
|---|---|---|
| Host check | Done | Debian 13, i5-6500T (4 cores), 16 GB RAM, 66 GB free, Docker 29.9, Compose v5.6, Tailscale up (iPhone on tailnet). |
| AI CLI check | Done | `claude -p --tools "" --json-schema ...` returns validated JSON on the subscription (~2s, Haiku). `codex exec -s read-only --ephemeral` works too. Gemini CLI not installed. Terms for unattended personal use still need a read before the AI features are turned on (Phase 4). |
| Owner's Gmail, LinkedIn alerts, Telegram bot | Owner to do | See docs/SETUP.md |
| 2-week ATS survey | Owner to do | Paste job links into Jobs. The ATS is auto-detected. |

## Phase 1: Tracker, profile, Telegram

| Item | Status | Notes |
|---|---|---|
| Docker Compose app (FastAPI + React, single container) | Done | Bound to 127.0.0.1:8080 only. Non-root, read-only rootfs, all caps dropped, 512 MB limit. |
| DB migrations (Alembic, SQLite WAL) | Done | Run automatically at container start. |
| Owner login (first-run password, argon2, signed cookie, CSRF header, login throttle) | Done | Tested |
| About Me profile (sections, confirm/unconfirmed, source, history, edit, delete) | Done | Refuses SSNs, passwords, bank and passport numbers. |
| Job tracker (paste URL, dedupe, ATS detection, statuses, timeline, notes) | Done | |
| Resume upload (PDF only, SHA-256, read-only file, versions, text view) | Done | |
| Overview (counts, kill switch, questions, activity) | Done | Same `summary()` function feeds the dashboard and Telegram, so they always agree. |
| AI settings: provider + model per task, with fallback | Partial | Settings are stored and editable. No AI calls exist yet; they arrive with Phase 3/4. |
| Telegram bot (owner lock, /status /pause /unpause /questions, answers to profile, daily summary) | Done (unit-tested with a fake Telegram) | Needs the owner's token to go live. |
| Tailscale Serve | Owner to do | `sudo tailscale serve --bg 8080` |

## Phase 4 (first slice): apply to one job

| Item | Status | Notes |
|---|---|---|
| Browser container (Chromium + noVNC) | Done | Watch/take over at `https://mini.tailb5b23c.ts.net:8443` (tailnet only, VNC password in `.env`). Chrome's own sandbox is off; the container is the boundary (non-root, no caps, 2 GB limit). |
| AI runner (host, Unix socket) | Done | `airunner/runner.py`. All Codex/Claude tools disabled; verified Codex answers `NO_TOOLS` when asked to read files. Install as a service: `scripts/install-ai-runner.sh`. |
| Form reader + AI plan + code checks | Done | AI picks field IDs and values; code rejects anything not backed by a confirmed fact (asks you instead), wrong select options, password fields. Submit is detected in code. |
| Telegram questions mid-application | Done | Answers are saved to About Me and reused next time. |
| Approval before submit (screenshot) | Done | Always. Dry run stops before submit. |
| Evidence | Done | Screenshot of every page and the confirmation page; "confirmed" only when a confirmation page is seen. |
| End-to-end tests | Done | Fake Workday-like site: sign in -> create account -> emailed code -> sign in -> custom dropdown + Add work experience -> approve -> confirmed. Also: job page -> Apply -> form (text, selects, radios, checkbox, resume upload) -> approval -> submit -> confirmed. Prompt-injection text in the page was ignored. |
| Real ATS sites (Jobvite first) | Next | First live run on the First Tech job. |
| Job-site accounts (create, sign in) | Done | One shared random password (owner's choice), AES-GCM encrypted, key only in `.env`. Never sent to the AI/Telegram/logs. Settings shows it. |
| Email verification (code or link) | Done | Reads the job Gmail inside JobPilot's browser (no App password needed; IMAP also supported). Only fresh mail from the same site/ATS. |
| Custom dropdowns, "Add" buttons | Done | ARIA combobox/listbox (Workday-style) read and filled; reveal-clicks capped. |
| Bot-trap fields, cookie banners | Done | Honeypot fields hidden from the AI and never filled; cookie banners declined. |
| Real Workday check | Done (read-only) | Workday's own careers site: sign-in and create-account forms read correctly. Nothing submitted. |
| CAPTCHA | By design | You get a Telegram ping to solve it (no bypassing). |

## Tests

`./scripts/test.sh`: 137 backend tests (incl. the plan safety checker) (auth, CSRF, throttle, session invalidation, dedupe, ATS
detection incl. lookalike domains, profile guards, resume immutability, Telegram owner-lock,
button and reply answers, SSN refusal, daily summary once/day, settings validation).
Frontend: strict TypeScript typecheck in the build.

Manual smoke test on a throwaway container: setup → add job → add fact → upload PDF →
overview → container restart → data and login still there.

## Next up (as of 2026-11-07)

1. **Set up job alerts on the job Gmail** (LinkedIn, Indeed, and company career sites like
   Workday/Greenhouse "job alerts"). As of 2026-11-07 the inbox has no alert emails, so
   discovery has nothing to read yet. Gmail is signed in again and the scanner works.
2. Keep fixing new ATS sites as real runs hit them.
3. Ideas, not started: start a batch automatically after a scan finds jobs; send unclear
   employer emails to AI (the `email_classifier` task) or a review queue; follow
   Glassdoor/ZipRecruiter alert links (skipped now: they're tracking redirects).

Already done (this list was stale before): fit check before applying, browser autofill,
the vault, and automatic Submit. Two real applications were submitted and confirmed on
2026-11-07: First Tech (Talemetry) and Farmers Mutual Hail (Greenhouse).
Backups: not wanted.

## 2026-11-07 updates
- Resume upload is done by code: any empty resume/CV file box (hidden or in a frame) gets the
  resume directly, verified, logged as "Attached your resume". Cover letter/photo boxes are skipped.
- Pages that show a loading spinner or "Processing..." are waited on (up to 90s) instead of
  asking the owner. Step progress bars ("Step 4 of 9") don't count.
- Common questions (About Me card, or Telegram /setup): ~30 standard application questions
  answered once, saved under standard labels (app/common_info.py).
- Page questions go to Telegram as ONE message; the reply is read by AI (app/info_answers.py)
  into standard facts (an address -> street/city/state/ZIP/country), resolving references to
  earlier replies. The planner matches any site's wording to these facts.
- Auto-submit (setting `auto_submit`, on by default): Submit is pressed without waiting for a tap;
  Telegram gets a "Submitting" screenshot. After Submit it waits up to 90s for the site, reads any
  error messages back to the AI, tries a DOM click on the 2nd try, and asks only after 3 silent tries.
- Pages are filled first, then remaining questions are asked. Optional fields are never asked about.
- Checkbox groups ("What is your desired salary?" + range boxes) are one question; hidden helper
  inputs (react-select's required input) are ignored.

## Batch runs and job discovery (2026-11-07)

| Item | Status | Notes |
|---|---|---|
| Batch runs | Done | "Apply to the next N jobs". N = `batch_size` (Settings, Jobs page, or Telegram `/batch 8`, `/batchsize 8`). Interested jobs first, then New, newest first. Only submitted applications count toward N; poor fits and failures don't, capped at 3*N tries. Never LinkedIn URLs; never jobs whose last try errored (retry those by hand). One Telegram summary at the end. Dashboard Stop or /pause stops it. Code: `app/apply/batch.py`. |
| Job discovery from job-alert emails | Done (tested with sample emails) | Every `discovery_interval_minutes` (default 60) and on `/scan` or Settings "Check now": reads unread mail (inbox + Updates/Promotions/Social tabs) in the job Gmail. Alert emails (LinkedIn, Indeed, company career sites) -> new jobs, deduped. Only links that are themselves a job posting on a known ATS/board are kept; redirects are never followed. Code: `app/discovery.py`. |
| Employer replies -> timelines | Done (rules only) | Linked only when exactly one sent application matches (employer name, or the ATS domain if only one job uses it). Received -> Acknowledged, rejection -> Rejected, interview -> Interview (Telegram alert right away). Assessments are alerted too. Unclear emails go on the timeline only. |
| Browser sharing | Done | An application run and the email scan take turns using the browser (one lock). |
| LinkedIn leads | By design | Added as New jobs for you to apply to yourself (no LinkedIn automation). Batches skip them. |
| Gmail "show original" fix | Done | Gmail began returning 404 for the old link; it now needs the account key (`ik`). Without it only a short preview was read. Also: Greenhouse mail (`greenhouse-mail.io`) and Jobvite mail for Talemetry portals now count as the same site. Verified live: the First Tech and Farmers "thanks for applying" emails moved both jobs to Acknowledged. |
| Job details from the link | Done | Paste just a link: employer, title and location are read from the posting in the background (schema.org JobPosting data, then page tags, then JobPilot's browser for JavaScript sites, then AI on the page text). Only blank fields are filled. Also runs for jobs found in emails, and from "Look up details" on a job. Never fetches LinkedIn; the plain download refuses private/LAN/Tailscale addresses. Verified live on BAE Systems (all 3 fields from page data). Code: `app/job_details.py`. |
| Sign-in pages with a "create account" link (BrassRing / BAE) | Fixed | The page reader skipped links like "Don't have an account yet?" (a `#0` link that runs JavaScript), so the AI saw no way forward. Links that act as buttons and account words (create, register, sign up) are now shown. Social sign-in buttons (LinkedIn, Google, Facebook, ...) are refused in code. Also: invisible (opacity 0) spinners no longer count as "still loading", which had cost 90-180s per step. |
| AI sees every page | Done | The planner now gets the screenshot on every step (before: only when stuck), plus the element list. The page reader also lists anything clickable (styled labels/spans, tabs, menu items), and the prompt tells the AI to work like a person: fix the site's error messages first, open dialogs/sections with clicks, use "Add résumé" then "Browse" (file pickers get the resume automatically), pick real dropdown options. Direction (owner, 2026-11-07): general abilities, not per-site rules. |
| Security questions | Done | Owner's rule: JobPilot picks the questions and makes up readable answers (e.g. "maple-harbor-42"), the same per site and question, recorded in the job's history (`account.security_answers`). Never his real info, never asked. |
| What we entered (per application) | Done | Every filled field is recorded per page (`apply.filled`): field, value, and where it came from (profile, AI-written, standing rule, made up, account). Shown under a job's Details on the Jobs page. The shared password and emailed codes are not written into it. |
| BAE Systems (BrassRing) | Submitted, confirmed (2026-11-07) | Third real confirmed application. Fixes along the way are all general: page reader shows button-like links and clickable labels; invisible spinners ignored; AI sees the screenshot every step; made-up security answers; typeahead dropdowns typed key by key with shorter searches and the real choices reported back; a failed field gets 3 tries before the owner is asked; a second AI check looks for the answer in saved facts before any question is sent. |
| Gemini | Installed, not signed in | Gemini CLI 0.67 + Node 22 in ~/.local (Node 26 needs libatomic, not installed). The AI runner runs it with every tool denied by policy (`toolName = "*"` deny) and JobPilot's own system prompt; screenshots aren't passed to Gemini. Owner signs in from Settings -> Gemini -> Connect Gemini (runner drives Google's sign-in in a pseudo-terminal; owner pastes Google's code). Settings offers its models after sign-in; no effort setting. |
| Effort selector | Fixed | "Default" model now offers the effort levels all that provider's models support. |
| Rosendin Electric (Workday) | Submitted, confirmed (2026-11-08) | Fourth real confirmed application, first Workday one. General fixes on the way: page reads time out instead of hanging (a frozen Workday tab had hung the browser); Workday search-and-pick boxes treated as dropdowns, with nested categories ("Career Websites > Corporate Website"), Enter-to-search, and "already chosen" checks; placeholder rows ("No Items.") ignored; dropdown questions read from the surrounding fieldset/form field instead of the button's own value label; "did it take?" checks only the box itself; header/nav menus ignored; account-wrecking buttons (Delete my information, Change email, Settings) refused in code. |
| Gmail session | Watch | Google signed the job Gmail out twice on 2026-11-07 (once after a browser restart). Cause not confirmed. |
| CHKD (symplr / HealthcareSource) | Blocked by the site | Account sign-up fails server-side every time ("An error occurred while updating the entries"). Ruled out: our security answers (short plain ones fail too), an existing account (email not registered), the password (`%` removed, then a password with `!@*`; still fails). Job set back to Interested. Fixes from it: "Finish" on account pages isn't the application submit; same error 3x after Submit -> stop and tell the owner; marketing Yes/No (radio/dropdown) answered No even when the site pre-picks Yes; generated passwords use only `!@*-_` and always include one of `!@*`. Shared password rotated twice on 2026-11-08 (existing accounts keep their own saved passwords). |
| Amazon (amazon.jobs) | Submitted, confirmed (2026-11-08) | Data Center Technician. REQUIRED online assessment by Tue Dec 8 2026 (owner does it). Fixes on the way: verification email from another company domain (amazon.dev vs amazon.jobs) + newest-fresh-code fallback + AI email read; open a link directly when clicking does nothing or keeps returning to the same page; two screenshots per step (viewport + full page); AI can click by screenshot position (code checks what's under the point); Select2/Chosen dropdowns chosen on the hidden real <select>, or typed into their own search box; highlighted != chosen; partial option lists searched instead of asked; "United States"/USA match the country, not outlying islands; Yes/No radios with the question glued to the option label; education level enforced from "Highest level of education completed" (Some college -> High school or equivalent), including wrong pre-filled values; short Telegram replies to one question always saved; re-check after filling; AI review of the whole application before Submit; data-sharing "AI preference" opt-ins answered No; vanished Submit button after a successful submit no longer crashes. |

## Browser agent (2026-11-08)

| Item | Status | Notes |
|---|---|---|
| Adaptive browser agent (observe -> act -> verify -> recover) | Done, **off by default** | Settings -> Automation -> "Smart browser agent" (`automation.browser_agent`). Off = previous behavior exactly. Design, safety and limits: `docs/BROWSER_AGENT.md`. Code: `app/apply/observe.py`, `tools.py`, `agent.py`, hooks in `worker.py`. |
| AI accessibility snapshots | Done | Playwright 1.63 `aria_snapshot(mode="ai", boxes=True)`; refs acted on with `aria-ref=` locators (no `get_by_ref` in 1.63). Valid only for the latest look. Works in cross-origin iframes. |
| Validated browser actions | Done | click (normal/dom/keyboard), click_at (screenshot pixels, checked first), fill, type_text, press_key, select_option, check/uncheck, scroll, focus, inspect, inspect_options, wait, upload_resume. Structured feedback (found/actionable/executed/verified/what changed/category/hint). |
| Hybrid routing | Done | Fast page plan first. Agent only for: a fill that failed or didn't stick, a question the planner sees but the reader missed (`other_controls`, fact-checked), a page with no progress / no plan. Bounded: 2 tries per field, 2 per page, 8 decisions each. |
| Progress detection | Done (flag on) | Values, errors, dialogs, active step and new sections count; URL fragments, clocks and banners don't. |
| Duplicate-submit guard | Done (flag on) | A second Submit press only if nothing happened or the site shows errors; a changed page with no confirmation and nothing to fill stops as "submitted, not confirmed". |
| Troubleshooting view | Done | Jobs -> job details -> "Browser agent": what it tried, what failed and why. |
| Real-browser tests | Done | `./scripts/test-browser.sh` (also run by `./scripts/test.sh`): 48 tests, headless Chromium vs local fixtures. Plus 32 new unit tests. |
| Benchmark | Done | `./scripts/test-browser.sh bench` (stand-in model) and `--live` (owner's model via the AI runner). Results in `docs/BROWSER_AGENT.md`. |
| Real ATS sites with the flag on | Next | Not tried yet. Turn the flag on and watch the next runs. |
| Fixes that help both paths | Done | Nested dropdown answers keep their "Category > Choice" path in check_plan; react-select "single value" boxes are read; "Text me about other job openings" counts as marketing. |

