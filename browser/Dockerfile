# JobPilot browser: a real, visible Chromium you can watch and take over from your
# phone (noVNC), which the app drives over the Chrome DevTools Protocol.
FROM debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium xvfb x11vnc novnc websockify socat openbox \
      fonts-liberation fonts-noto-core fonts-noto-color-emoji ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 browser
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

USER browser
ENV DISPLAY=:99
EXPOSE 6080 9223
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
