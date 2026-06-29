#!/usr/bin/env bash
# register-project.sh — bind an existing GitHub repo as a Switchboard project.
# Creates projects/<slug>/{project.env,WORKFLOW.md} and the gate-state labels on
# the repo's issue board. Does NOT clone the repo (that happens per-ticket, at run
# time, in the workspace-population hook). Idempotent: safe to re-run to upgrade.
#
# Usage:
#   scripts/register-project.sh --slug acme-api --repo acme/api [--base main]
#                               [--max-agents 4] [--workspace-base /srv/switchboard/workspaces]
#                               [--convention-root <dir>] [--self]
#
# --convention-root <dir>  Root a project's .switchboard/ and .decisions/ under <dir>
#                          instead of the repo root. Used for dogfooding so this repo
#                          can manage itself without polluting the general-purpose root.
# --self                   Convenience: convention-root=self, slug defaults to
#                          'switchboard-self'. Still pass --repo <you>/switchboard.
set -euo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SLUG="" REPO="" BASE="main" MAX_AGENTS="4" CONVENTION_ROOT=""
WORKSPACE_BASE="${SB_WORKSPACE_BASE:-/srv/switchboard/workspaces}"

while [ $# -gt 0 ]; do
  case "$1" in
    --slug)            SLUG="$2"; shift 2;;
    --repo)            REPO="$2"; shift 2;;
    --base)            BASE="$2"; shift 2;;
    --max-agents)      MAX_AGENTS="$2"; shift 2;;
    --workspace-base)  WORKSPACE_BASE="$2"; shift 2;;
    --convention-root) CONVENTION_ROOT="$2"; shift 2;;
    --self)            CONVENTION_ROOT="self"; [ -n "$SLUG" ] || SLUG="switchboard-self"; shift 1;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

[ -n "$SLUG" ] || { echo "ERROR --slug required" >&2; exit 2; }
[ -n "$REPO" ] || { echo "ERROR --repo owner/name required" >&2; exit 2; }
[[ "$REPO" == */* ]] || { echo "ERROR --repo must be owner/name" >&2; exit 2; }
command -v gh >/dev/null || { echo "ERROR gh CLI not found" >&2; exit 1; }

PROJ_DIR="$SB_HOME/projects/$SLUG"
WORKSPACE_ROOT="$WORKSPACE_BASE/$SLUG"
mkdir -p "$PROJ_DIR" "$WORKSPACE_ROOT"

# Convention prefix: "" for root projects, "<dir>/" otherwise (e.g. "self/").
CONVENTION_PREFIX=""
if [ -n "$CONVENTION_ROOT" ]; then
  CONVENTION_PREFIX="${CONVENTION_ROOT%/}/"
  # Scaffold the project's convention dirs inside this repo so the clone has them.
  mkdir -p "$SB_HOME/${CONVENTION_PREFIX}.switchboard/intents" "$SB_HOME/${CONVENTION_PREFIX}.decisions"
  touch "$SB_HOME/${CONVENTION_PREFIX}.switchboard/intents/.gitkeep" \
        "$SB_HOME/${CONVENTION_PREFIX}.decisions/.gitkeep"
  echo "convention root: ${CONVENTION_PREFIX} (project artifacts isolated here)"
fi

# --- 1. binding -------------------------------------------------------------
cat > "$PROJ_DIR/project.env" <<EOF
# Switchboard project binding for '$SLUG'. Sourced and exported by run-project.sh
# so the workspace hooks can see it. Secrets stay in the environment, not here.
SB_PROJECT_SLUG=$SLUG
SB_GITHUB_REPO=$REPO
SB_BASE_BRANCH=$BASE
SB_WORKSPACE_ROOT=$WORKSPACE_ROOT
SB_CONVENTION_ROOT=$CONVENTION_PREFIX
# GITHUB_TOKEN is expected from the environment (GitHub App installation token).
EOF

# --- 2. composed WORKFLOW.md (base + substitutions) -------------------------
sed \
  -e "s|{{REPO}}|$REPO|g" \
  -e "s|{{WORKSPACE_ROOT}}|$WORKSPACE_ROOT|g" \
  -e "s|{{MAX_AGENTS}}|$MAX_AGENTS|g" \
  -e "s|{{CONVENTION_ROOT}}|$CONVENTION_PREFIX|g" \
  "$SB_HOME/workflow/WORKFLOW.base.md" > "$PROJ_DIR/WORKFLOW.md"

# --- 3. gate-state labels on the repo ---------------------------------------
mklabel() { # name color description
  gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force >/dev/null 2>&1 \
    && echo "  label $1" || echo "  label $1 (exists/skipped)"
}
echo "creating gate-state labels on $REPO:"
mklabel "status:drafting"     "FBCA04" "Gate A: intent/spec being authored (not dispatched)"
mklabel "status:todo"         "0E8A16" "Approved & dispatchable"
mklabel "status:in-progress"  "1D76DB" "Agent working"
mklabel "status:plan-review"  "D93F0B" "Gate B: plan/ADR awaiting approval (not dispatched)"
mklabel "status:human-review" "5319E7" "Gate C: implementation done, awaiting human merge"
mklabel "status:blocked"      "B60205" "Parked / dependency unmet"

cat <<EOF

registered '$SLUG' -> $REPO
  binding:    $PROJ_DIR/project.env
  workflow:   $PROJ_DIR/WORKFLOW.md
  workspaces: $WORKSPACE_ROOT

next:
  SB_ORCHESTRATOR_CMD="<your generated orchestrator launch cmd>" scripts/run-project.sh $SLUG
EOF
