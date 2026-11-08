#!/bin/sh
# Start: virtual screen -> window manager -> Chromium -> VNC -> noVNC (web) -> CDP relay.
set -eu

W=${SCREEN_WIDTH:-1280}
H=${SCREEN_HEIGHT:-900}


rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 "${W}x${H}x24" -nolisten tcp &
sleep 1
openbox >/dev/null 2>&1 &

# Listen on localhost only; websockify (noVNC) is the only way in, and noVNC is only
# published on 127.0.0.1 and reached through Tailscale Serve (your tailnet only).
# Optional password: set VNC_PASSWORD to require one (kept off a command line).
if [ -n "${VNC_PASSWORD:-}" ]; then
  mkdir -p "$HOME/.vnc"
  x11vnc -storepasswd "$VNC_PASSWORD" "$HOME/.vnc/passwd" >/dev/null 2>&1
  VNC_AUTH="-rfbauth $HOME/.vnc/passwd"
else
  VNC_AUTH="-nopw"
fi
# shellcheck disable=SC2086
x11vnc -display :99 $VNC_AUTH -forever -shared -localhost -quiet -noxdamage &

websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/dev/null 2>&1 &

# Chrome only binds DevTools to 127.0.0.1; relay it to the private Docker network so
# the app container can reach it. Not published to the host.
socat TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222 &

# Keep Chromium running; restart it if it's closed or crashes.
# --no-sandbox: Chrome's own sandbox needs user namespaces, which Docker's seccomp
# policy blocks for a non-root, no-capabilities container. The container itself is
# the isolation boundary here (non-root, all caps dropped, no-new-privileges, memory
# limit, nothing published but noVNC on localhost). Same approach as Playwright's images.
while true; do
  rm -f /profile/SingletonLock /profile/SingletonSocket /profile/SingletonCookie
  chromium \
    --user-data-dir=/profile \
    --no-sandbox --test-type \
    --remote-debugging-port=9222 \
    --no-first-run --no-default-browser-check \
    --disable-dev-shm-usage \
    --disable-features=Translate,MediaRouter \
    --password-store=basic \
    --window-position=0,0 --window-size="${W},${H}" \
    --start-maximized \
    about:blank || true
  sleep 2
done
