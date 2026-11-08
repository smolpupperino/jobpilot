#!/usr/bin/env bash
# Connect JobPilot to your job-search Gmail (for verification codes/links and as the
# login for job-site accounts). Asks for the address and a Google *App password*
# (hidden as you type), tests the login, saves both to .env, restarts the app.
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env found."; exit 1; }

read -rp "Job-search Gmail address: " ADDR
ADDR="${ADDR//[[:space:]]/}"
[[ "$ADDR" =~ ^[^@]+@(gmail|googlemail)\.com$ ]] || { echo "That doesn't look like a Gmail address."; exit 1; }
echo "Paste the 16-letter App password (Google Account > Security > App passwords)."
echo "It won't show as you type:"
read -rs APW; echo
APW="${APW//[[:space:]]/}"
[ ${#APW} -eq 16 ] || { echo "App passwords are 16 letters. Nothing changed."; exit 1; }

echo "Testing the login..."
if ! ADDR="$ADDR" APW="$APW" python3 -I - <<'PY'
import imaplib, os, sys
try:
    with imaplib.IMAP4_SSL("imap.gmail.com", timeout=30) as m:
        m.login(os.environ["ADDR"], os.environ["APW"])
        m.select("INBOX", readonly=True)
except Exception as e:
    print(f"Login failed: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(1)
PY
then
  echo "Nothing changed. Check the address and App password (2-Step Verification must be on)."
  exit 1
fi
echo "Login works."

ADDR="$ADDR" APW="$APW" python3 -I - .env <<'PY'
import os, sys
path = sys.argv[1]
want = {"JOBPILOT_GMAIL_ADDRESS": os.environ["ADDR"], "JOBPILOT_GMAIL_APP_PASSWORD": os.environ["APW"]}
lines, seen = [], set()
for line in open(path):
    key = line.split("=", 1)[0].strip()
    if key in want:
        line, _ = f"{key}={want[key]}\n", seen.add(key)
    lines.append(line)
for key in want.keys() - seen:
    lines.append(f"{key}={want[key]}\n")
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.writelines(lines)
os.replace(tmp, path)
PY
chmod 600 .env
unset APW
echo "Saved. Restarting JobPilot..."
docker compose up -d --force-recreate app >/dev/null
echo "Done. JobPilot will use $ADDR for job-site accounts and verification emails."
