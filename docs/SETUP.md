# Setup: your morning checklist

JobPilot is already running on the mini PC (`docker ps` shows `jobpilot`). These steps
connect it to you. Total time is about 30 minutes.

## 1. Open the dashboard on your iPhone (5 min)

On the mini PC:

```bash
sudo tailscale serve --bg 8080
tailscale serve status        # shows your URL, like https://mini.<something>.ts.net
```

If it complains about HTTPS, turn on **MagicDNS** and **HTTPS Certificates** in the
Tailscale admin console (DNS page), then run it again.

On your iPhone (Tailscale app connected), open that URL in Safari. If
`JOBPILOT_TAILSCALE_OWNER_LOGIN` in `.env` is your Tailscale login, you're signed in
automatically (Tailscale vouches for who you are). Otherwise you'll see **"First run.
Create the owner password."**

If the page says "server can't be found", check for a custom DNS profile on the phone
(Settings → General → VPN, DNS & Device Management). One like AdGuard DNS overrides
Tailscale's DNS. Set DNS to Automatic, or add AdGuard as a global nameserver in the
Tailscale admin console instead.

Tip: Safari → Share → **Add to Home Screen** makes it feel like an app.

> The dashboard is only reachable from devices on your tailnet. It is not on the
> public internet or your home Wi-Fi.

## 2. Telegram bot (10 min)

**Shortcut:** after creating the bot, run `~/jobpilot/scripts/setup-telegram.sh`. It asks for
the token (hidden), has you message the bot to find your user ID, saves both to `.env`, and
restarts the app. That replaces steps 3–5 below.

1. In Telegram, message **@BotFather** → `/newbot` → pick a name, e.g. `NathanJobPilotBot`.
2. It gives you a **token**. Don't paste it in any chat (including Claude).
3. On the mini PC:
   ```bash
   cd ~/jobpilot
   nano .env          # paste the token after JOBPILOT_TELEGRAM_BOT_TOKEN=
   docker compose up -d --force-recreate
   ```
4. Open your bot in Telegram and send `hi`. It replies with **your user ID**.
5. Put that number after `JOBPILOT_TELEGRAM_OWNER_ID=` in `.env`, then
   `docker compose up -d --force-recreate` again.
6. Send `/status` to the bot. You should get a status report back.
7. In the dashboard, go to **Settings → Telegram → Send a test question**. Tap an
   answer in Telegram, and it shows up on the **About Me** page under *Application
   answers*. (It's a real saved answer, so edit or delete it afterward if you like.)

## 3. Job-search Gmail (10 min)

1. Create the new Gmail account.
2. Turn on **2-Step Verification** (Google Account → Security).
3. Create an **App password** (Google Account → Security → App passwords). Save it in
   your password manager. JobPilot will need it in Phase 2 (reading email); don't
   put it anywhere yet.
4. Update the email on your resume to the new address and export the PDF.

## 4. LinkedIn job alerts

In LinkedIn, set your **email** to the new Gmail (or add it as the alert address), then
create job alerts for your target titles (SOC analyst, security analyst, IT security,
etc.), with **email** notifications on, daily.

## 5. In the dashboard

- **Resume**: upload the PDF with the new email.
- **About Me**: add your contact info, work history, certifications, work
  authorization, and preferences. Only *confirmed* facts are ever used.
- **Jobs**: whenever you see a job you'd apply to, paste the link. JobPilot detects
  which application system it uses (Workday, iCIMS, ...). After 2 weeks this tells us
  which system to automate first.

## 6. Applying with JobPilot

1. **Install the AI runner once** (it runs Codex/Claude for JobPilot, tools off):
   `~/jobpilot/scripts/install-ai-runner.sh` (asks for your sudo password once).
2. **Watch the browser** from your phone: `https://mini.tailb5b23c.ts.net:8443`.
   The password is `JOBPILOT_VNC_PASSWORD` in `~/jobpilot/.env`
   (`grep VNC ~/jobpilot/.env`). Save it in Safari.
3. In **Jobs**, tap **Apply with JobPilot** on a job. Follow along on Overview
   (Watch / Stop) and Telegram. Keep **Dry run** on for your first tries.
4. It asks you on Telegram for anything not in About Me (answers are saved), and
   always asks before submitting, with a screenshot.
5. **Job Gmail:** put the address in `.env` as `JOBPILOT_GMAIL_ADDRESS=...`, then in
   **Settings -> Job-site accounts & Gmail** tap **Sign in to Gmail in JobPilot's
   browser** and sign in there (via the viewer). No App password needed. JobPilot
   uses that Gmail to create job-site accounts and reads verification emails from it.

## Everyday commands (on the mini PC)

```bash
cd ~/jobpilot
docker compose ps                  # is it running?
docker compose logs -f --tail 50   # live logs
docker compose up -d --build       # update after code changes
docker compose down                # stop
./scripts/test.sh                  # run the test suite
```

Emergency stop: the **Pause everything** button on Overview, or `/pause` on Telegram.
