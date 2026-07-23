#!/usr/bin/env bash
# Inject exactly one typed Codex availability failure per issue workspace, then
# delegate every later invocation to the real Codex CLI. This is an inert
# Stage 7 mixed-canary fixture; production workflows never reference it.
set -euo pipefail

RUN_DIR="$PWD/.run"
FAILURE_MARKER="$RUN_DIR/stage7-circuit-failure-injected"
mkdir -p "$RUN_DIR"

if (set -C; : >"$FAILURE_MARKER") 2>/dev/null; then
  printf '%s\n' \
    '{"type":"thread.started","thread_id":"stage7-circuit-injected-outage"}' \
    '{"type":"error","error":{"code":"service_unavailable","message":"deterministic mixed-canary provider outage"}}'
  exit 1
fi

if [ -n "${SWITCHBOARD_CANARY_CODEX_BIN:-}" ]; then
  REAL_CODEX="$SWITCHBOARD_CANARY_CODEX_BIN"
elif command -v codex >/dev/null 2>&1; then
  REAL_CODEX="$(command -v codex)"
else
  REAL_CODEX="/Applications/ChatGPT.app/Contents/Resources/codex"
fi

[ -x "$REAL_CODEX" ] || {
  printf 'ERROR: real Codex CLI is not executable: %s\n' "$REAL_CODEX" >&2
  exit 127
}

exec "$REAL_CODEX" "$@"
