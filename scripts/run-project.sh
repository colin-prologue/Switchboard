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

# issue #32: capture the sha this process is about to LOAD, before anything can
# move HEAD. Captured here (not at exec time) so every caller of the freshness
# preflight below sees it bound — pinning it later left the launch path always
# unbound and silently degraded to a HEAD-based detector, which self-clears on
# the operator's own `git pull` while pre-pull code is still running.
# Non-fatal by construction: outside a work tree `rev-parse` exits 128, and
# under `set -euo pipefail` an unguarded capture would turn every repo-less
# deploy into a hard launch refusal — the inverse of the fail-open thesis. The
# stderr redirect keeps git's own "fatal: not a git repository" line from
# preceding the preflight's named warning.
SB_LAUNCH_SHA="$(git -C "$SB_HOME" rev-parse HEAD 2>/dev/null || true)"
[ -n "$SB_LAUNCH_SHA" ] && export SB_LAUNCH_SHA || true

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

# issue #32: recompose config from origin and surface stale loaded code. Runs
# BEFORE the cd below, so the preflight anchors everything to $SB_HOME itself.
# It exits 0 on every fail-open path; a non-zero here is a usage error and
# SHOULD refuse the launch under `set -e`.
COMPOSED_WORKFLOW="$SB_HOME/.run/$SLUG/composed-WORKFLOW.md"
# Invoked via `bash` rather than as an executable so the launch path does not
# depend on the exec bit surviving a copy/checkout.
bash "$SB_HOME/scripts/freshness-preflight.sh" "$SLUG"

# Launch is the one moment a restart-needed signal is cheaply actionable.
RESTART_MARKER="$SB_HOME/.run/$SLUG/restart-needed.json"
if [ -f "$RESTART_MARKER" ]; then
  echo "[run-project] restart-needed marker present ($RESTART_MARKER):"
  cat "$RESTART_MARKER"
fi

# The documented SB_ORCHESTRATOR_CMD ("uv run --project orchestrator ...") is
# relative to the repo root — pin the cwd so launching from anywhere works.
cd "$SB_HOME"

# issue #32: the exec path is UNCONDITIONALLY the composed file. The fallback
# is content-level (the preflight seeds this path from the tracked WORKFLOW.md
# when it does not yet exist), never path-level — a conditional path would move
# the file the scheduler stats once at startup, so a later recompose would go
# unwatched.
#
# LAST RESORT (codex review, PR #140): when the composed file STILL does not
# exist — .run/ unwritable, so neither recompose nor seeding can ever create
# it — launch from the tracked workflow rather than aborting a checkout that
# worked read-only before #32. The unconditional-path rationale does not
# apply here: with .run/ unwritable there is nothing to hot-reload, so no
# recompose can go unwatched.
if [ ! -f "$COMPOSED_WORKFLOW" ]; then
  echo "WARN composed workflow missing at $COMPOSED_WORKFLOW (.run unwritable?) — launching from tracked $WORKFLOW; hot config reload is INACTIVE" >&2
  COMPOSED_WORKFLOW="$WORKFLOW"
fi
# shellcheck disable=SC2086
exec $SB_ORCHESTRATOR_CMD --workflow "$COMPOSED_WORKFLOW"
