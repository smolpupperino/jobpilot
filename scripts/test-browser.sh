#!/usr/bin/env sh
# Real-browser tests (headless Chromium against local fixture pages) and the benchmark.
# Runs in a throwaway test image; never touches JobPilot's live browser or real sites.
#   scripts/test-browser.sh                 # browser tests
#   scripts/test-browser.sh bench [args]    # benchmark (see tests/browser/bench.py)
set -e
cd "$(dirname "$0")/.."
docker build -q -t jobpilot-test-browser:local -f backend/tests/browser/Dockerfile backend >/dev/null
if [ "$1" = "bench" ]; then
  shift
  exec docker run --rm -v "$PWD/backend":/src:ro ${BENCH_DOCKER_ARGS:-} -w /tmp jobpilot-test-browser:local sh -c \
    "cp -r /src /w && cd /w && PYTHONPATH=/w python -m tests.browser.bench $*"
fi
exec docker run --rm -v "$PWD/backend":/src:ro -w /tmp jobpilot-test-browser:local sh -c \
  "cp -r /src /w && cd /w && PYTHONPATH=/w python -m pytest -q -p no:cacheprovider tests/browser $*"
