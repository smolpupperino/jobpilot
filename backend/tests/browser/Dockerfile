# Test-only image: Python + Debian's Chromium, for the real-browser tests in tests/browser.
# Never connects to JobPilot's live browser.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      chromium python3 python3-venv fonts-liberation ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /venv
ENV PATH=/venv/bin:$PATH JOBPILOT_TEST_CHROMIUM=/usr/bin/chromium
COPY pyproject.toml /deps/pyproject.toml
RUN mkdir -p /deps/app && touch /deps/app/__init__.py && pip install -q "/deps[dev]" \
    && pip uninstall -q -y jobpilot && rm -rf /root/.cache /deps
