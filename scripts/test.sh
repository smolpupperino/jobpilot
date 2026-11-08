#!/usr/bin/env sh
# Run the backend test suite + lint in a throwaway container (no local Python packages needed).
set -e
cd "$(dirname "$0")/.."
docker run --rm -v "$PWD/backend":/src:ro -w /tmp python:3.12-slim sh -c \
  'cp -r /src /w && cd /w && pip install -q --root-user-action=ignore -e ".[dev]" >/dev/null 2>&1 && python -m pytest -q -p no:cacheprovider && ruff check --no-cache app tests && echo "lint ok"'
# Real-browser tests (headless Chromium vs local fixture pages; ~4 min). SKIP_BROWSER=1 skips them.
if [ -z "$SKIP_BROWSER" ]; then
  ./scripts/test-browser.sh
fi
