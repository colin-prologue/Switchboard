#!/usr/bin/env bash
# run-project.sh — launch ONE project's Symphony process (N-process topology:
# one orchestrator process per registered project, sharing the installed runtime).
#
# Usage:
#   SB_ORCHESTRATOR_CMD="uv run --project orchestrator python -m orchestrator" \
#     scripts/run-project.sh <slug>
#
# Sources projects/<slug>/project.env and exports its SB_* vars so the workspace
# hooks (hooks/*.sh) can see them, then execs the orchestrator with the project's
# composed WORKFLOW.md. GITHUB_TOKEN must already be in the environment.
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
: "${GITHUB_TOKEN:?GITHUB_TOKEN not set (tracker auth; for dogfood: export GITHUB_TOKEN=\"\$(gh auth token)\")}"

set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

echo "[run-project] $SLUG -> $SB_GITHUB_REPO (workspaces: $SB_WORKSPACE_ROOT)"
mkdir -p "$SB_WORKSPACE_ROOT"

# shellcheck disable=SC2086
exec $SB_ORCHESTRATOR_CMD --workflow "$WORKFLOW"
