#!/usr/bin/env bash
# Build the image and prove what it ships with.
#
# Every assertion here is about the artefact an operator would actually deploy,
# not about the Dockerfile's text: it runs, it runs as a non-root user, it
# serves the compiled UI, it has every capability switched off, it returns its
# security headers, its root filesystem is read-only, and it can complete a real
# run with no credentials at all.
#
#   scripts/smoke-container.sh                 # docker, published base images
#   ENGINE=podman scripts/smoke-container.sh   # podman
#   NODE_IMAGE=... PYTHON_IMAGE=... scripts/smoke-container.sh
#   KEEP=1 scripts/smoke-container.sh          # leave it running to point a
#                                              # browser at the shipped image
set -euo pipefail

cd "$(dirname "$0")/.."

ENGINE="${ENGINE:-docker}"
IMAGE="${IMAGE:-devlens:smoke}"
NAME="${NAME:-devlens-smoke}"
PORT="${PORT:-8300}"
BUILD_ARGS=()
[ -n "${NODE_IMAGE:-}" ] && BUILD_ARGS+=(--build-arg "NODE_IMAGE=$NODE_IMAGE")
[ -n "${PYTHON_IMAGE:-}" ] && BUILD_ARGS+=(--build-arg "PYTHON_IMAGE=$PYTHON_IMAGE")

command -v "$ENGINE" >/dev/null || { echo "$ENGINE is not installed" >&2; exit 1; }

cleanup() { "$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true; }
# KEEP leaves the container up so the browser suite can run against the image
# itself rather than a working copy. The caller owns the teardown then.
if [ -z "${KEEP:-}" ]; then trap cleanup EXIT; fi
"$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true

step() { printf '\n\033[1m── %s\033[0m\n' "$1"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$1" >&2; "$ENGINE" logs "$NAME" 2>&1 | tail -30 >&2; exit 1; }

step "Build"
"$ENGINE" build "${BUILD_ARGS[@]}" -t "$IMAGE" .

step "Run, constrained the way Compose runs it"
# tmpfs-mode=1777 because /data is prepared in the image and a fresh tmpfs
# replaces it with a root-owned directory the unprivileged user cannot write.
"$ENGINE" run -d --name "$NAME" \
  --read-only --cap-drop=ALL --security-opt=no-new-privileges \
  --pids-limit=256 --memory=1g \
  --tmpfs /tmp:rw,noexec,nosuid,size=32m \
  --mount type=tmpfs,destination=/data,tmpfs-mode=1777 \
  -p "127.0.0.1:$PORT:8000" "$IMAGE" >/dev/null

step "Health"
for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" | grep -q '"status":"ok"' || fail "the container never became healthy"
curl -fsSI "http://127.0.0.1:$PORT/health" >/dev/null || fail "HEAD /health is not answered"

step "It does not run as root"
"$ENGINE" exec "$NAME" id -u | grep -qv '^0$' || fail "the container runs as root"

step "It serves the compiled UI"
curl -fsS "http://127.0.0.1:$PORT/" | grep -qi "<title>" || fail "GET / did not return the UI"

step "Every capability is off"
curl -fsS "http://127.0.0.1:$PORT/capabilities" > /tmp/devlens-capabilities.json
python3 - <<'PY' || fail "a capability is enabled in a bare image"
import json, sys
body = json.load(open("/tmp/devlens-capabilities.json"))
if body["enabled"]:
    sys.exit(f"enabled: {body['enabled']}")
for name in ("git_writeback", "repository_code_execution", "llm", "service_discovery"):
    assert name in body["denied"], name
PY

step "Security headers"
curl -fsSI "http://127.0.0.1:$PORT/health" | tr 'A-Z' 'a-z' > /tmp/devlens-headers.txt
for header in x-content-type-options x-frame-options content-security-policy referrer-policy; do
  grep -q "$header" /tmp/devlens-headers.txt || fail "missing $header"
done

step "The root filesystem is read-only and /data is not"
"$ENGINE" exec "$NAME" sh -c 'touch /usr/local/probe' 2>/dev/null && fail "the root filesystem is writable"
"$ENGINE" exec "$NAME" sh -c 'touch /data/probe && rm /data/probe' || fail "/data is not writable"

step "It explains an unconfigured deployment rather than failing opaquely"
curl -s "http://127.0.0.1:$PORT/ready" | grep -q "DEVLENS_JIRA_URL" || fail "/ready does not name what is missing"

step "The tools the workflows shell out to are present"
"$ENGINE" exec "$NAME" git --version >/dev/null || fail "git is missing"
"$ENGINE" exec "$NAME" rg --version >/dev/null || fail "ripgrep is missing"

step "A real run completes with no credentials"
JOB=$(curl -fsS -X POST "http://127.0.0.1:$PORT/jobs" \
  -H 'content-type: application/json' -H "origin: http://127.0.0.1:$PORT" \
  -d '{"command":"ask","question":"what does idempotency mean for a retry?"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
for _ in $(seq 1 60); do
  STATUS=$(curl -fsS "http://127.0.0.1:$PORT/jobs/$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [ "$STATUS" = "completed" ] && break
  [ "$STATUS" = "failed" ] && fail "the run failed inside the container"
  sleep 1
done
[ "$STATUS" = "completed" ] || fail "the run did not complete (status: $STATUS)"
curl -fsS "http://127.0.0.1:$PORT/jobs/$JOB" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin)["result"]; assert any("deterministic" in i for i in d["limitations"]), d["limitations"]' \
  || fail "the report did not carry its limitations"

printf '\n\033[1;32mThe image is what it claims to be.\033[0m\n'
if [ -n "${KEEP:-}" ]; then
  printf 'Still running as %s on http://127.0.0.1:%s — remove it with: %s rm -f %s\n' \
    "$NAME" "$PORT" "$ENGINE" "$NAME"
fi
