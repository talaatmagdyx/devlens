#!/usr/bin/env bash
# Every gate, in the order CI runs them.
#
# This script is the definition of "verified" for DevLens: CI runs exactly these
# commands, so a green run here and a green run there mean the same thing. Run
# it before you push and you will not be surprised.
#
#   scripts/verify.sh            # everything except the live provider contract
#   scripts/verify.sh --live     # also call the real GitHub API
#   scripts/verify.sh --fast     # skip the browser suite and the load tests
#   scripts/verify.sh --soak 30  # add a 30-minute sustained soak at the end
set -euo pipefail

cd "$(dirname "$0")/.."

LIVE=0
FAST=0
SOAK=0
while [ $# -gt 0 ]; do
  case "$1" in
    --live) LIVE=1 ;;
    --fast) FAST=1 ;;
    --soak) SOAK="${2:?--soak needs a duration in minutes}"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

PYTHON="${PYTHON:-python3}"
[ -x .venv/bin/python ] && PYTHON=.venv/bin/python

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }

# Every capability is off and every credential is absent, because that is the
# state DevLens ships in and the state its guarantees are about.
unset $(env | grep -oE '^(DEVLENS|ANTHROPIC|OPENAI|CODEX|CLAUDE)_[A-Z_]*' || true) 2>/dev/null || true

step "Missing tools"
for tool in git rg; do
  command -v "$tool" >/dev/null || { echo "$tool is required and not on PATH" >&2; exit 1; }
done

step "Lint"
"$PYTHON" -m ruff check .

step "Types"
"$PYTHON" -m mypy

step "Backend tests"
if [ "$FAST" = 1 ]; then
  "$PYTHON" -m pytest -q --deselect tests/test_under_load.py
else
  "$PYTHON" -m pytest -q --cov=devlens --cov-report=term-missing
fi

if [ "$LIVE" = 1 ]; then
  step "Live provider contract"
  "$PYTHON" -m pytest -q tests/test_live_contract.py -rs
fi

step "Frontend types"
(cd web && npm run typecheck)

step "Frontend tests"
(cd web && npm run test)

step "Frontend build"
(cd web && npx vite build)

step "No external host in the bundle"
if grep -rEl "https?://(cdn|unpkg|jsdelivr)" web/dist/ >/dev/null 2>&1; then
  echo "the built bundle references an external host" >&2
  exit 1
fi

if [ "$FAST" = 0 ]; then
  step "Browser end to end"
  (cd web && npx playwright test)
fi

if [ "$SOAK" != 0 ]; then
  step "Sustained soak (${SOAK} minutes)"
  # A real process driven continuously and sampled while it runs. The question
  # a burst cannot answer: do descriptors, memory and the database stay flat
  # over time, or does something climb until the host notices.
  SOAK_DIR="$(mktemp -d)"
  DEVLENS_JOBS_DB="$SOAK_DIR/jobs.sqlite" DEVLENS_ANALYZE_ROOT="$SOAK_DIR" \
    "$PYTHON" -m uvicorn devlens.app.api:app --host 127.0.0.1 --port 8121 \
    > "$SOAK_DIR/server.log" 2>&1 &
  SOAK_PID=$!
  trap 'kill "$SOAK_PID" 2>/dev/null || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:8121/health >/dev/null 2>&1 && break
    sleep 1
  done
  "$PYTHON" scripts/soak.py --base http://127.0.0.1:8121 --pid "$SOAK_PID" \
    --db "$SOAK_DIR/jobs.sqlite" --minutes "$SOAK" --report "$SOAK_DIR/soak.json"
  kill "$SOAK_PID" 2>/dev/null || true
  echo "soak report: $SOAK_DIR/soak.json"
fi

printf '\n\033[1;32mAll gates passed.\033[0m\n'
