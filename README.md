# JobPilot

A self-hosted job application assistant. It runs on a home mini PC in Docker, and you use
it from your phone over Tailscale. It tracks jobs, keeps a profile of verified facts about
you, asks you questions on Telegram, and (in later phases) fills out applications in a
visible browser using a hybrid design: **the AI reads forms and plans, code fills them in**.

- **Spec:** [SPEC.md](SPEC.md) (the working spec); `docs/SPEC-original.md` is the long original
- **Setup:** [docs/SETUP.md](docs/SETUP.md)
- **What's built:** [docs/STATUS.md](docs/STATUS.md)

## Quick start

```bash
cp .env.example .env && chmod 600 .env   # then set JOBPILOT_SESSION_SECRET
docker compose up -d --build
sudo tailscale serve --bg 8080           # private HTTPS on your tailnet
```

## Principles

- No fabricated facts. Only owner-confirmed profile facts are used on applications.
- Never stores SSNs, passwords in the profile, or bank or passport numbers.
- No CAPTCHA/MFA bypass and no LinkedIn scraping.
- "Submitted" requires evidence.
- No public ports. The dashboard is on localhost and published only via Tailscale.
- Job pages and emails are untrusted input. AI output is validated before anything acts on it.

## Layout

```
backend/   FastAPI app, SQLite + Alembic, Telegram bot, tests
frontend/  React + TypeScript dashboard (mobile-first)
docs/      setup, status, original spec
```
