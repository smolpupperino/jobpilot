#!/usr/bin/env bash
# Install / restart the JobPilot AI runner as a systemd user service.
# Lingering lets it run without you being logged in (needs sudo once).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p local-run && chmod 700 local-run
mkdir -p ~/.config/systemd/user
for d in ~/.claude ~/.codex ~/.cache ~/.local/state ~/.local/share/claude; do mkdir -p "$d"; done
touch ~/.claude.json
cp airunner/jobpilot-ai.service ~/.config/systemd/user/jobpilot-ai.service
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes; then
  sudo loginctl enable-linger "$USER"
fi
systemctl --user daemon-reload
systemctl --user enable --now jobpilot-ai.service
systemctl --user restart jobpilot-ai.service
sleep 1
systemctl --user --no-pager status jobpilot-ai.service | head -5
