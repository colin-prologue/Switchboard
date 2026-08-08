#!/usr/bin/env bash
# Inject exactly one typed Codex availability failure per issue workspace, then
# delegate every later invocation to the real Codex CLI. This is an inert
# Stage 7 mixed-canary fixture; production workflows never reference it.
set -euo pipefail

RUN_DIR="$PWD/.run"
FAILURE_MARKER="$RUN_DIR/stage7-circuit-failure-injected"
mkdir -p "$RUN_DIR"

if (set -C; : >"$FAILURE_MARKER") 2>/dev/null; then
  # Real-shaped injection (issue #109): actual codex-cli 0.146.0-alpha.3.1
  # failure events carry NO error.code — turn.failed nests error.message only,
  # and intermediate error events use a top-level message. The message text
  # below is the REAL captured 401 string (orchestrator/tests/fixtures/
  # codex_cli_auth_401.jsonl) plus an injection marker; classification must
  # travel the _TEXT_PATTERNS path, the only path real Codex traffic takes.
  printf '%s\n' \
    '{"type":"thread.started","thread_id":"stage7-circuit-injected-outage"}' \
    '{"type":"turn.started"}' \
    '{"type":"turn.failed","error":{"message":"unexpected status 401 Unauthorized: Missing bearer or basic authentication in header, url: https://api.openai.com/v1/responses (mixed-canary injected outage)"}}'
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
