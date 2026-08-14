#!/usr/bin/env bash
# register-project.sh — bind an existing GitHub repo as a Switchboard project.
# Creates projects/<slug>/{project.env,WORKFLOW.md} and the gate-state labels on
# the repo's issue board. Does NOT clone the repo (that happens per-ticket, at run
# time, in the workspace-population hook). Idempotent: safe to re-run to upgrade.
#
# Usage:
#   scripts/register-project.sh --slug acme-api --repo acme/api [--base main]
#                               [--stance prototype|harden|sustain|base]
#                               [--verify-cmd "<command>"] [--verify-tools '<allowlist>']
#                               [--max-agents 4] [--workspace-base ~/Developer/switchboard-workspaces]
#                               [--convention-root <dir>] [--self]
#
# --stance <name>          Which workflow recipe to compose from. Determines how much
#                          discipline the project runs under; see workflow/stances/.
#                          Default 'prototype' (the loose end of the ladder) so new
#                          projects start fast by construction. 'base' selects the
#                          legacy pre-stance template at workflow/WORKFLOW.base.md.
#                          Re-run with a different --stance to promote/demote a project;
#                          the orchestrator hot-reloads the recomposed WORKFLOW.md.
# --verify-cmd <command>   The project's "does it run" command, substituted into the
#                          stance's preflight and QA sections. Project-specific by
#                          nature: a test suite, a build, a headless run.
# --verify-tools <list>    The --allowedTools entries the worker needs to run that
#                          command, as a quoted list, e.g. '"Bash(godot:*)"'. Without
#                          this the verify command is denied at runtime and the
#                          session strands.
# --convention-root <dir>  Root a project's .switchboard/ and .decisions/ under <dir>
#                          instead of the repo root. Used for dogfooding so this repo
#                          can manage itself without polluting the general-purpose root.
# --self                   Convenience: convention-root=self, slug defaults to
#                          'switchboard-self'. Still pass --repo <you>/switchboard.
set -euo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SLUG="" REPO="" BASE="main" MAX_AGENTS="4" CONVENTION_ROOT="" IS_SELF=0
STANCE="prototype" VERIFY_CMD="" VERIFY_TOOLS=""
WORKSPACE_BASE="${SB_WORKSPACE_BASE:-$HOME/Developer/switchboard-workspaces}"

while [ $# -gt 0 ]; do
  case "$1" in
    --slug)            SLUG="$2"; shift 2;;
    --repo)            REPO="$2"; shift 2;;
    --base)            BASE="$2"; shift 2;;
    --stance)          STANCE="$2"; shift 2;;
    --verify-cmd)      VERIFY_CMD="$2"; shift 2;;
    --verify-tools)    VERIFY_TOOLS="$2"; shift 2;;
    --max-agents)      MAX_AGENTS="$2"; shift 2;;
    --workspace-base)  WORKSPACE_BASE="$2"; shift 2;;
    --convention-root) CONVENTION_ROOT="$2"; shift 2;;
    --self)            CONVENTION_ROOT="self"; IS_SELF=1; [ -n "$SLUG" ] || SLUG="switchboard-self"; shift 1;;
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

# Stance -> template. 'base' keeps the pre-stance path for already-registered
# projects; everything else resolves under workflow/stances/, with a
# project-local override winning so a project can carry a recipe the shared
# ladder does not cover (the pattern projects/mixed-canary/ already uses).
case "$STANCE" in
  base) TEMPLATE="$SB_HOME/workflow/WORKFLOW.base.md";;
  *)
    if [ -f "$PROJ_DIR/WORKFLOW.$STANCE.md" ]; then
      TEMPLATE="$PROJ_DIR/WORKFLOW.$STANCE.md"
    else
      TEMPLATE="$SB_HOME/workflow/stances/WORKFLOW.$STANCE.md"
    fi
    ;;
esac
[ -f "$TEMPLATE" ] || {
  echo "ERROR unknown stance '$STANCE' (no $TEMPLATE)" >&2
  echo "available:" >&2
  ls "$SB_HOME/workflow/stances/" 2>/dev/null | sed -n 's/^WORKFLOW\.\(.*\)\.md$/  \1/p' >&2
  echo "  base" >&2
  exit 2
}

# Convention prefix: "" for root projects, "<dir>/" otherwise (e.g. "self/").
CONVENTION_PREFIX=""
if [ -n "$CONVENTION_ROOT" ]; then
  CONVENTION_PREFIX="${CONVENTION_ROOT%/}/"
  # Scaffold the convention dirs ONLY for --self, where SB_HOME *is* the managed
  # repo. For an external project the dirs belong in that project's repo, which
  # agents create inside their workspace clone — creating them under SB_HOME
  # would silently scaffold them into Switchboard instead.
  if [ "$IS_SELF" = "1" ]; then
    mkdir -p "$SB_HOME/${CONVENTION_PREFIX}.switchboard/intents" "$SB_HOME/${CONVENTION_PREFIX}.decisions"
    touch "$SB_HOME/${CONVENTION_PREFIX}.switchboard/intents/.gitkeep" \
          "$SB_HOME/${CONVENTION_PREFIX}.decisions/.gitkeep"
  fi
  echo "convention root: ${CONVENTION_PREFIX} (project artifacts isolated here)"
fi

# --- 1. binding -------------------------------------------------------------
cat > "$PROJ_DIR/project.env" <<EOF
# Switchboard project binding for '$SLUG'. Sourced and exported by run-project.sh
# so the workspace hooks can see it. Secrets stay in the environment, not here.
SB_PROJECT_SLUG=$SLUG
SB_WORKFLOW_STANCE=$STANCE
SB_GITHUB_REPO=$REPO
SB_BASE_BRANCH=$BASE
SB_WORKSPACE_ROOT=$WORKSPACE_ROOT
SB_CONVENTION_ROOT=$CONVENTION_PREFIX
SB_VERIFY_CMD=$VERIFY_CMD
SB_VERIFY_TOOLS=$VERIFY_TOOLS
# GITHUB_TOKEN is expected from the environment (GitHub App installation token).
EOF

# --- 2. composed WORKFLOW.md (stance template + substitutions) --------------
# '|' is the sed delimiter, so a '|' inside a verify command would break the
# expression; reject it rather than emit a corrupt workflow.
case "$VERIFY_CMD$VERIFY_TOOLS" in
  *"|"*) echo "ERROR --verify-cmd/--verify-tools cannot contain '|'" >&2; exit 2;;
esac
sed \
  -e "s|{{REPO}}|$REPO|g" \
  -e "s|{{WORKSPACE_ROOT}}|$WORKSPACE_ROOT|g" \
  -e "s|{{MAX_AGENTS}}|$MAX_AGENTS|g" \
  -e "s|{{CONVENTION_ROOT}}|$CONVENTION_PREFIX|g" \
  -e "s|{{BASE_BRANCH}}|$BASE|g" \
  -e "s|{{VERIFY_CMD}}|$VERIFY_CMD|g" \
  -e "s|{{VERIFY_TOOLS}}|$VERIFY_TOOLS|g" \
  "$TEMPLATE" > "$PROJ_DIR/WORKFLOW.md"

# --- 3. gate-state labels on the repo ---------------------------------------
mklabel() { # name color description
  # --force makes this idempotent (existing labels are updated, never an
  # error) — so ANY failure here is real (bad repo, no auth, no permission)
  # and must abort registration, not print a fake "(exists/skipped)".
  local out
  if out=$(gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force 2>&1); then
    echo "  label $1"
  else
    echo "ERROR creating label $1 on $REPO:" >&2
    echo "$out" >&2
    exit 1
  fi
}
echo "creating gate-state labels on $REPO:"
mklabel "status:drafting"     "FBCA04" "Gate A: intent/spec being authored (not dispatched)"
mklabel "status:triage"       "006B75" "Adversarial ticket verification before dispatch"
mklabel "status:todo"         "0E8A16" "Approved & dispatchable"
mklabel "status:in-progress"  "1D76DB" "Agent working"
mklabel "status:plan-review"  "D93F0B" "Gate B: plan/ADR awaiting approval (not dispatched)"
# #55: waiting-on-operator gate. Triage routes here on a NEEDS DECISION verdict
# (the ticket is blocked on an unmade human decision, which a verifier must not
# make). Gate BY OMISSION from active_states — no orchestrator code reads it.
mklabel "status:decision"     "C2E0C6" "Waiting on operator: triage asked a Gate-A question (not dispatched)"
mklabel "status:human-review" "5319E7" "Gate C: implementation done, awaiting human merge"
# Agent QA state (stance ladder). ACTIVE in the prototype stance: the terminal
# handoff lands here and a QA session reviews the diff and merges, escalating to
# status:human-review only on the escalation list. Gated stances do not list it
# in active_states, so registering it costs nothing there.
mklabel "status:review"       "8A63D2" "Agent QA: PR open, awaiting review by a reviewer session"
# C2 (2026-07-05): status:blocked is advisory only — the orchestrator gates on
# GitHub-native blocked-by, NOT this label. Reworded so it no longer collides
# with status:parked (cap-park). Human/board-managed; the dispatch guard ignores it.
mklabel "status:blocked"      "B60205" "Advisory only (human/board-managed); orchestrator gates on native blocked-by, not this label"
mklabel "status:parked"       "E99695" "Cap-park: orchestrator halted at session cap — remove to re-dispatch"
# Provenance marker (issue #29): applied automatically by triage on PASS. Its
# presence is the durable proof an issue passed triage; the dispatch guard
# refuses to claim a status:todo that lacks it. Not a status:* state.
mklabel "gate:triage-passed"  "0E8A16" "Provenance: promoted by triage (PASS). Dispatch guard requires it on status:todo"

cat <<EOF

registered '$SLUG' -> $REPO
  stance:     $STANCE  ($TEMPLATE)
  verify:     ${VERIFY_CMD:-<none set — preflight will be skipped with a note>}
  binding:    $PROJ_DIR/project.env
  workflow:   $PROJ_DIR/WORKFLOW.md
  workspaces: $WORKSPACE_ROOT

next:
  SB_ORCHESTRATOR_CMD="<your generated orchestrator launch cmd>" scripts/run-project.sh $SLUG
EOF
