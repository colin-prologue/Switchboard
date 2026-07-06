#!/usr/bin/env bash
# run-project.sh — launch ONE project's Symphony process (N-process topology:
# one orchestrator process per registered project, sharing the installed runtime).
#
# Usage:
#   SB_ORCHESTRATOR_CMD="uv run --project orchestrator python -m orchestrator" \
#     scripts/run-project.sh <slug>
#
# Sources ~/.config/switchboard/app.env (GitHub App identity, issue #10) and
# projects/<slug>/project.env, exporting their vars so the orchestrator and the
# workspace hooks (hooks/*.sh) can see them, then execs the orchestrator with
# the project's composed WORKFLOW.md. Credentials: a complete SB_APP_* set
# (preferred) or GITHUB_TOKEN (dogfood fallback).
set -euo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SB_HOME

SLUG="${1:-}"
[ -n "$SLUG" ] || { echo "usage: scripts/run-project.sh <slug>" >&2; exit 2; }

ENV_FILE="$SB_HOME/projects/$SLUG/project.env"
WORKFLOW="$SB_HOME/projects/$SLUG/WORKFLOW.md"
[ -f "$ENV_FILE" ] || { echo "ERROR no such project '$SLUG' ($ENV_FILE missing) — register it first" >&2; exit 1; }
[ -f "$WORKFLOW" ] || { echo "ERROR $WORKFLOW missing — re-run register-project.sh" >&2; exit 1; }

: "${SB_ORCHESTRATOR_CMD:?SB_ORCHESTRATOR_CMD not set (see SETUP.md Stage 3)}"

# issue #10: the App credential set (non-secret identifiers; the SECRET is the
# .pem they reference by path) lives outside the repo. Source it if present.
APP_ENV="$HOME/.config/switchboard/app.env"
if [ -f "$APP_ENV" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$APP_ENV"
  set +a
  echo "[run-project] App identity: ${SB_APP_BOT_LOGIN:-<unset>} (from $APP_ENV)"
fi

# All five keys (minting trio + bot identity pair) — a partial set must not
# silently half-switch identities; the orchestrator enforces the same rule.
if [ -z "${GITHUB_TOKEN:-}" ] && { [ -z "${SB_APP_ID:-}" ] \
    || [ -z "${SB_APP_INSTALLATION_ID:-}" ] || [ -z "${SB_APP_PRIVATE_KEY_FILE:-}" ] \
    || [ -z "${SB_APP_BOT_LOGIN:-}" ] || [ -z "${SB_APP_BOT_USER_ID:-}" ]; }; then
  echo "ERROR no credentials: provide $APP_ENV with SB_APP_ID/SB_APP_INSTALLATION_ID/SB_APP_PRIVATE_KEY_FILE/SB_APP_BOT_LOGIN/SB_APP_BOT_USER_ID (preferred), or export GITHUB_TOKEN (dogfood: \"\$(gh auth token)\")" >&2
  exit 1
fi

set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

echo "[run-project] $SLUG -> $SB_GITHUB_REPO (workspaces: $SB_WORKSPACE_ROOT)"
mkdir -p "$SB_WORKSPACE_ROOT"

# The documented SB_ORCHESTRATOR_CMD ("uv run --project orchestrator ...") is
# relative to the repo root — pin the cwd so launching from anywhere works.
cd "$SB_HOME"

# shellcheck disable=SC2086
exec $SB_ORCHESTRATOR_CMD --workflow "$WORKFLOW"
