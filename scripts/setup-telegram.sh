#!/usr/bin/env bash
# Connect JobPilot to your Telegram bot in one step.
# Asks for the bot token (hidden as you type), finds your Telegram user ID when you
# message the bot, writes both into .env, and restarts the app.
# The token never appears on screen, in shell history, or in the process list.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=.env
[ -f "$ENV_FILE" ] || { echo "No .env found in $(pwd). Copy .env.example first."; exit 1; }

tg() {  # tg <method> [query]: call the Bot API without putting the token in argv
  printf 'url = "https://api.telegram.org/bot%s/%s%s"\n' "$TOKEN" "$1" "${2:+?$2}" \
    | curl -sS --max-time 40 -K -
}

echo "Paste your bot token from @BotFather and press Enter (it won't show as you type):"
read -rs TOKEN
echo
TOKEN="${TOKEN//[[:space:]]/}"
if ! [[ "$TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
  echo "That doesn't look like a bot token (it should look like 123456:ABC...). Nothing changed."
  exit 1
fi

ME=$(tg getMe)
if [ "$(jq -r '.ok' <<<"$ME")" != "true" ]; then
  echo "Telegram rejected that token: $(jq -r '.description // "unknown error"' <<<"$ME")"
  echo "Nothing changed. If you just used /revoke, make sure you copied the NEW token."
  exit 1
fi
BOT=$(jq -r '.result.username' <<<"$ME")
echo "Token works. Bot: @$BOT"

# Stop the app while we listen, so its bot doesn't grab your message first.
echo "Pausing JobPilot for a moment..."
docker compose stop app >/dev/null 2>&1 || true

# Skip any old messages so we only see the one you're about to send.
LAST=$(tg getUpdates "offset=-1&timeout=0" | jq -r '.result[-1].update_id // empty')
OFFSET=$(( ${LAST:-0} + 1 ))

echo
echo ">>> Now open Telegram and send any message (like: hi) to @$BOT"
echo "    Waiting up to 5 minutes..."
OWNER_ID=""
for _ in $(seq 1 10); do
  UPD=$(tg getUpdates "offset=$OFFSET&timeout=30&allowed_updates=%5B%22message%22%5D")
  MSG=$(jq -c '[.result[] | select(.message.chat.type == "private")][0] // empty' <<<"$UPD")
  if [ -n "$MSG" ]; then
    OWNER_ID=$(jq -r '.message.from.id' <<<"$MSG")
    NAME=$(jq -r '[.message.from.first_name, .message.from.last_name, (.message.from.username // empty | "@"+.)] | map(select(. != null)) | join(" ")' <<<"$MSG")
    OFFSET=$(( $(jq -r '.update_id' <<<"$MSG") + 1 ))
    break
  fi
done

if [ -z "$OWNER_ID" ]; then
  echo "Didn't see a message. Restarting JobPilot unchanged. Run this script again when ready."
  docker compose up -d >/dev/null
  exit 1
fi

tg getUpdates "offset=$OFFSET&timeout=0" >/dev/null  # mark it as read
echo "Got a message from: $NAME (user ID $OWNER_ID)"
read -rp "Is that you? Only this account will be able to control JobPilot. [y/N] " OK
if [[ ! "$OK" =~ ^[Yy] ]]; then
  echo "Okay, nothing changed. Restarting JobPilot unchanged."
  docker compose up -d >/dev/null
  exit 1
fi

# Write both values into .env (passed via environment, not command-line args).
TOKEN="$TOKEN" OWNER_ID="$OWNER_ID" python3 - "$ENV_FILE" <<'PY'
import os, sys
path = sys.argv[1]
want = {"JOBPILOT_TELEGRAM_BOT_TOKEN": os.environ["TOKEN"], "JOBPILOT_TELEGRAM_OWNER_ID": os.environ["OWNER_ID"]}
lines, seen = [], set()
for line in open(path):
    key = line.split("=", 1)[0].strip()
    if key in want:
        line = f"{key}={want[key]}\n"
        seen.add(key)
    lines.append(line)
for key in want.keys() - seen:
    lines.append(f"{key}={want[key]}\n")
tmp = path + ".tmp"
with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
    f.writelines(lines)
os.replace(tmp, path)
PY
chmod 600 "$ENV_FILE"
unset TOKEN

echo "Saved to .env. Starting JobPilot..."
docker compose up -d --force-recreate >/dev/null
for _ in $(seq 1 30); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' jobpilot 2>/dev/null)" = "healthy" ] && break
  sleep 2
done
echo "JobPilot is $(docker inspect -f '{{.State.Health.Status}}' jobpilot 2>/dev/null || echo 'starting')."
echo
echo "Done! Send /status to @$BOT in Telegram to test it."
