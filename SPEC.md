# JobPilot — v1 Spec (trimmed)

Version: 1.1 | Updated: 2026-11-05
Owner: Nathan
Status: Planning. Nothing is built yet.

This is the working spec. The original long spec lives in
`docs/SPEC-original.md`. Read this file first. Where the two disagree, **this
file wins**. Use the original for details this one doesn't cover (the test
matrix, threat model, status wording, event fields and so on).

---

## 1. What JobPilot is

A self-hosted job application assistant that runs on a Debian mini PC in Docker
and is used from an iPhone or desktop over Tailscale. It:

1. Collects job leads (LinkedIn alert emails, pasted URLs, public job feeds).
2. Filters and ranks them against my real profile using rules, with AI only
   when needed.
3. Fills out applications in a visible Chromium browser.
4. Messages me on **Telegram** when it needs an answer, an approval, or my
   hands (CAPTCHA, MFA, unknown question).
5. Records exactly what was submitted, and links later employer emails to the
   right application.

**Target:** about 20 quality applications a day, in cybersecurity, SOC, IT
security and adjacent roles.

## 2. Decisions made (changes from the original spec)

| Topic | Original spec | v1 decision |
|---|---|---|
| Resume | Light AI tailoring of a template | **No tailoring.** Upload the owner's own PDF, unchanged, every time. Store its SHA-256 hash and record that hash on every application. |
| Human-in-the-loop | Dashboard plus noVNC only | **Telegram bot** is the main notification and approval channel. The dashboard and noVNC handle takeover. |
| Auto-submit | Up to fully autonomous | **Default: prepare, then approve with one tap on Telegram.** Autonomous submit only for specific site/employer combinations that I've whitelisted later. |
| AI providers | Claude, Codex and Gemini with a fallback router | **One provider at a time, $0 budget.** Use subscription-backed CLIs (`claude -p`, `gemini`, `codex exec`) as text-only adapters, or a free-tier API key. Verify each one's terms and limits in Phase 0. No complex router. |
| Email | Gmail OAuth read-only | **Dedicated job-search Gmail**, read over IMAP with an app password. (Gmail OAuth apps in "testing" mode lose their refresh tokens after 7 days, which is painful for a personal app.) |
| Concurrency | 1 to 6 browser contexts | **One browser worker at a time**, as a sequential queue. Raise it only if a benchmark shows headroom and the queue is actually backing up. |
| Event store | Append-only, integrity hashes, projections | **Plain `events` table** plus normal tables. |
| Encryption | Field-level encryption | **Full-disk encryption (LUKS)** on the host, `chmod 600` secrets, and encrypted backups. Only ATS account passwords get field encryption. |
| Chat assistant | Main interface, built early | **Built last.** The Telegram bot and dashboard come first, and chat is a layer on top of the same data. |
| ATS adapter order | Greenhouse, Lever, Ashby, and Workday later | **Decided by data.** Count the ATS mix in 2 weeks of real leads, then build adapters in that order. Workday is probably first. |
| Learning loop | Not present | **Add one:** callback rate by source, by ATS, by title cluster and by fit score. Flag ghost jobs and staffing-agency reposts. |

## 3. Telegram bot (new)

The channel for everything that needs me.

**Message types:**
- **Approval:** "Ready: SOC Analyst I, Acme Health (Workday). Fit 82. 14 fields
  filled from profile, 0 guessed." Buttons: `[Submit] [Skip] [View]`.
- **Question:** "Acme asks: 'Willing to work rotating weekends?'" Buttons for
  the offered choices, or a free-text reply. The answer is saved to the answer
  library with `verification_status = owner_confirmed`, so it's never asked
  again. Every saved answer shows up on the **"About Me" profile page** in the
  dashboard (see section 3a), where I can view, edit or delete it.
- **Takeover needed:** "CAPTCHA on Acme's Workday." Includes a screenshot and a
  link to the noVNC takeover page (Tailscale URL), plus `[I'm done, resume]`.
- **Daily summary:** found, filtered, prepared, submitted (confirmed),
  submitted (uncertain), and waiting on me.
- **Email codes:** see section 4.

**Rules:**
- The bot only accepts messages from my Telegram user ID and ignores everyone
  else.
- Telegram bot chats are **not end-to-end encrypted.** Never send or accept
  SSNs, passwords, bank details, government ID numbers or security-question
  answers through Telegram. If a site asks for one, say "do this one manually
  in takeover."
- Security questions ("mother's maiden name") are **account credentials**, not
  profile facts. If an ATS account requires them, generate a random answer and
  store it with that site's password in the encrypted credential store. Never
  put it in the answer library.
- Questions time out. The application waits in `WAITING_FOR_OWNER` while the
  rest of the queue keeps going.
- Long polling (`getUpdates`), so there's no public webhook and no inbound port.

## 3a. "About Me" profile page

One page in the dashboard that shows everything JobPilot knows about me, and
I can edit all of it.

- **Sections:** contact info, work history, education, certifications, work
  authorization, preferences (location, pay, remote), and the **answer
  library**, meaning every application question I've answered.
- **Each entry shows:** the value, where it came from (resume import, my
  Telegram reply, typed in the dashboard), when I last confirmed it, and how
  many applications it's been used on.
- **Edit in place.** Changes are versioned, so old applications still show
  what was actually submitted at the time.
- Unconfirmed entries (for example, ones pulled from the resume) are flagged
  and never used until I confirm them.
- Credentials (site passwords and security-question answers) are **not**
  shown here. They live in the separate encrypted credential store, with a
  "reveal" button that requires re-login.

## 4. Dedicated Gmail inbox

- Create a new Gmail account just for job applications, and use it on every
  application and ATS account.
- JobPilot reads it over IMAP (read-only behavior: it never sends, never
  deletes).
- **Automated uses:**
  - Parse LinkedIn job-alert emails into leads (requires setting the alerts up
    on this address, or forwarding them to it).
  - Pull verification codes and email-verification links **only** when all of
    these are true: (a) the worker is waiting for one right now, (b) the sender
    domain matches the ATS/employer the worker is on, (c) it arrived within the
    last ~10 minutes. Anything else is ignored. Never click random links from
    email.
  - Classify replies (received, rejection, interview, assessment) with rules
    first and AI only for unclear ones. Link each to its application.
    Interviews and recruiter replies trigger an immediate Telegram alert.
- **Decided:** I'll update my resume PDF to show the new job Gmail, so
  recruiters reply to the inbox JobPilot watches. This is a contact-info
  change I make myself, not tailoring. The updated PDF becomes resume v1.

## 5. AI usage (no API budget, but plenty of subscription quota)

**Approach: hybrid. AI reads and decides, code clicks and types.**

I have a lot of subscription quota, so AI can be used freely where it helps.
But it isn't the whole engine. The split:

| Job | Who does it |
|---|---|
| Read a form page and work out what each field is asking | **AI.** It gets the page's field list (labels, types, options from the accessibility tree), not screenshots or pixels. |
| Decide which profile answer goes in each field | **AI**, but it may only choose from my confirmed profile/answer library or say "ask owner." It can never invent a value. |
| Actually fill fields, upload the resume, click Next | **Code (Playwright).** Fast and reliable. Every action is logged. |
| Operate a control the fast path can't (unfamiliar dropdown, popup, stuck page) | **AI, one small step at a time, through JobPilot's own validated browser tools** (setting `browser_agent`; see docs/BROWSER_AGENT.md). The values still come from the checked plan; code decides what's allowed and whether it worked. |
| Remember a form layout so it's quicker next time | **Code.** Cache the AI's field mapping per ATS and form, so repeat forms need no AI. |
| Read a job posting and judge fit | **AI**, in full. It explains missing and matching requirements against my profile, with rule-based hard filters first. |
| Classify employer emails | Rules for the obvious ones, AI for anything unclear. |
| Parse LinkedIn alerts, dedupe, count, run the dashboard, enforce safety gates | **Code only.** |
| Chat assistant | **AI**, using read-only DB tools (last phase). |

Why this split: an AI that clicks around by looking at screenshots is slow,
burns quota and makes random mistakes, which was ApplyTwin's problem. An AI
that **reads the form and outputs a plan, which code then executes**, is fast,
cheap on repeat forms, and easy to check before submitting.

Amended 2026-11-08 (owner's direction): the fast plan stays the default for ordinary
fields, but when it can't operate a control or the page is stuck, an adaptive agent
observes -> acts -> verifies -> recovers in small steps, with an accessibility snapshot,
targeted screenshots and a short memory of what already failed. It gets a fixed action set
(click, type, keys, choose, scroll, inspect...) executed and checked by code, never code,
selectors or a shell. It can't press the final Submit or decide facts.

This also means JobPilot can attempt **any** application site, not only ones
with a hand-built adapter. Hand-built adapters (Workday first) are still worth
writing for the most common sites, for speed and reliability.

**Hard rules for the AI:**
- Its output is a JSON plan, which is checked against a schema and against my
  profile before any code runs it.
- It never sees passwords, security answers or the credential store.
- Job pages and emails are untrusted text. The AI has no tools that let that
  text cause actions.
- Unknown question → Telegram question to me. Never a guess.

**Adapter approach:**
- Call a CLI in non-interactive mode with **all tools disabled** (text in,
  JSON out), in an empty temp directory with no access to secrets or the
  profile beyond what the prompt contains.
- Candidates, to verify in Phase 0:
  - `claude -p` using my Claude subscription
  - `gemini` CLI using a Google login (free tier)
  - `codex exec` using a ChatGPT login
  - Google AI Studio free API key. Note: free-tier data may be used for
    training, so send only redacted job text and never profile data.
- Why a CLI: paid API keys are billed separately from subscriptions. The
  official CLIs, signed in with my subscription, are how subscription quota
  gets used from a program. Phase 0 confirms which one allows this kind of
  personal automation and how high its limits are.
- Cache everything by hash. The same question text on the same ATS means no
  new AI call.
- If the provider is rate-limited, the task waits, and the queue keeps doing
  non-AI work. A simple fallback to a second signed-in CLI is fine if one is
  available. I can switch providers in settings.
- **Provider and model are chosen per task** (form mapper, job fit, email
  classifier, chat), each with an optional fallback.
- **Switching mid-application is seamless.** Every AI call is stateless: it
  gets the form or job text and returns a JSON plan. Application progress lives
  in the DB and the browser, never inside the AI. A call already running
  finishes on the old model, and the next call uses the new one.
- **Context never piles up.** Each call sends only what it needs: the form
  fields of one page plus the relevant profile facts, or one job post, or one
  email. That's a few thousand tokens, far below the window. Job posts and emails
  are trimmed to a size cap first. Chat is the only multi-turn task: it sends a
  short profile summary plus the last ~20 messages, and older turns are dropped
  (or summarized), so a chat can run forever without filling the window.

## 6. Resume

- One file: the owner's PDF, stored read-only, with its SHA-256 recorded.
- At setup, extract its text **once** to pre-fill the profile (employers,
  dates, certifications). I then review and confirm every fact in the profile
  editor. Unconfirmed facts are never used to answer questions.
- Every application records the hash of the exact PDF uploaded.
- Replacing the resume later creates a new version. Old applications keep a
  link to the version they used.

## 7. Phases

**Phase 0: Inventory and lead survey** (about 1 week, mostly waiting)
- Host check: CPU, RAM, disk, Docker, Tailscale.
- Verify the AI CLI options (section 5): auth, headless use, rate limits,
  terms.
- Create the job Gmail account and set up LinkedIn job alerts on it.
- Create the Telegram bot.
- Collect about 2 weeks of leads and count them by ATS.
- *Done when:* I know my RAM budget, which AI route works, and the top 3 ATSs
  in my leads.

**Phase 1: Tracker, profile and Telegram** (first useful version)
- Docker Compose, FastAPI, SQLite (WAL), React dashboard, login, Tailscale
  only.
- Profile and answer library editor, with facts pre-filled from the resume
  PDF and confirmed by me.
- Paste a job URL to add a job. Track status manually.
- Telegram bot: login lock to my ID, daily summary, test message.
- *Done when:* I can track my real applications from my phone and get a
  Telegram summary.

**Phase 2: Gmail ingestion**
- IMAP reader: LinkedIn alerts become leads (deduped), and employer replies
  are linked to applications.
- Unmatched or unclear emails go to a review queue.
- *Done when:* a week of real email is correctly sorted, with no wrong
  matches.

**Phase 3: Filters and ranking**
- Hard filters (location, clearance, sponsorship, seniority, excluded
  employers, closed or duplicate) and a rule-based fit score with reasons.
- *Done when:* the ranking of my real leads looks right to me.

**Phase 4: Browser autofill for the #1 ATS (probably Workday)**
- Visible Chromium on Xvfb, with noVNC takeover over Tailscale.
- Local mock site for tests first. Then real postings in **dry-run** (fill,
  screenshot, never submit).
- Pre-submit snapshot of every field. Telegram approval. Submit. Capture the
  confirmation page.
- ATS account creation: unique generated password in the encrypted store,
  and email verification via section 4.
- CAPTCHA, MFA or an unknown question pauses the application and sends a
  Telegram message.
- *Done when:* 10 real applications go through with correct evidence, no
  duplicates, and no wrong answers.

**Phase 5: Volume and more ATSs**
- Adapters #2 and #3 from the Phase 0 data.
- Optional whitelisted autonomous submit for well-proven employer/ATS pairs.
- Callback-rate stats.

**Phase 6: Chat assistant**
- Natural-language questions over the same DB through read-only query tools,
  plus a few safe commands (pause, resume, skip, queue). Reachable from the
  dashboard and Telegram.

## 8. Non-negotiables (unchanged from the original)

- No fabricated facts. Unknown stays unknown, so ask on Telegram.
- No LinkedIn scraping or automation. Alerts by email only.
- No CAPTCHA or MFA bypass. I do those myself in takeover.
- No SSN, ever. If one is requested, the application pauses for manual
  handling.
- "Submitted" requires confirmation evidence. Otherwise it's
  `SUBMISSION_UNCERTAIN`.
- Never blindly retry a final submit.
- Job posts and emails are untrusted data and can't trigger actions.
- No public ports. VNC and CDP stay internal, and the dashboard is reachable
  over Tailscale only.
- Emergency stop: one button on the dashboard and `/stop` on Telegram.

## 9. Portfolio angle

Keep the repo publishable from day one. No real data in git, plus
`examples/` with fake data, `SECURITY.md` and a `THREAT_MODEL.md`. A
security-focused job hunter showing a threat-modeled, least-privilege,
prompt-injection-aware project makes a good portfolio piece.

## 10. Open questions

1. ~~Personal inbox?~~ Decided: the new Gmail goes on the resume.
2. Which AI CLI or route actually works headlessly within its terms? (Phase 0)
3. Mini PC RAM and CPU?
4. Locations, remote preference, pay floor, excluded employers, clearance
   stance.
5. Is 20 a day realistic given how many entry-level security jobs actually
   exist in my area? The Phase 0 lead count answers this.
