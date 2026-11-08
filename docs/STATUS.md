# Status

Last updated: 2026-11-06

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

`./scripts/test.sh`: 79 backend tests (incl. the plan safety checker) (auth, CSRF, throttle, session invalidation, dedupe, ATS
detection incl. lookalike domains, profile guards, resume immutability, Telegram owner-lock,
button and reply answers, SSN refusal, daily summary once/day, settings validation).
Frontend: strict TypeScript typecheck in the build.

Manual smoke test on a throwaway container: setup → add job → add fact → upload PDF →
overview → container restart → data and login still there.

## Next up (as of 2026-11-07)

1. **Batch runs:** apply to a customizable number of jobs in one go, e.g. "apply to the next
   N jobs". Set N in Settings or from Telegram, run them one after another, and send a summary.
2. **Job discovery:** read job-alert emails (Gmail in JobPilot's browser) to add jobs
   automatically (employer replies feed the job timelines).
3. Keep fixing new ATS sites as real runs hit them.

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
