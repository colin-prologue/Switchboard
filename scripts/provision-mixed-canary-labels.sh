#!/usr/bin/env bash
# Provision every label required by the isolated Stage 6 mixed-provider canary.
# Idempotent: --force updates existing labels. This does not create issues or
# launch the orchestrator.
# Usage: scripts/provision-mixed-canary-labels.sh [--repo owner/name] [--dry-run]
set -euo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO=""
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo) REPO="$2"; shift 2;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help)
      sed -n '2,18p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

if [ -z "$REPO" ]; then
  REPO="$(sed -n 's/^SB_GITHUB_REPO=//p' \
    "$SB_HOME/projects/mixed-canary/project.env" | head -1)"
fi
[ -n "$REPO" ] || { echo "ERROR could not resolve mixed-canary repository" >&2; exit 2; }
[[ "$REPO" == */* ]] || { echo "ERROR --repo must be owner/name (got '$REPO')" >&2; exit 2; }

if [ "$DRY_RUN" -eq 0 ]; then
  command -v gh >/dev/null || { echo "ERROR gh CLI not found" >&2; exit 1; }
fi

provision() { # name color description
  if [ "$DRY_RUN" -eq 1 ]; then
    printf 'label %s\n' "$1"
    return
  fi
  gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force
  printf 'label %s\n' "$1"
}

printf 'repo: %s\n' "$REPO"
provision "status:drafting"     "FBCA04" "Gate A: intent/spec being authored (not dispatched)"
provision "status:triage"       "006B75" "Adversarial ticket verification before dispatch"
provision "status:todo"         "0E8A16" "Approved and dispatchable"
provision "status:in-progress"  "1D76DB" "Agent working"
provision "status:plan-review"  "D93F0B" "Gate B: plan/ADR awaiting approval (not dispatched)"
provision "status:human-review" "5319E7" "Gate C: implementation done, awaiting human merge"
provision "status:blocked"      "B60205" "Advisory only; orchestrator gates on native blocked-by"
provision "status:parked"       "E99695" "Cap-park: orchestrator halted at session cap"
provision "gate:triage-passed"  "0E8A16" "Provenance required on status:todo"
provision "agent:claude"        "8250DF" "Operator request for Claude before provider assignment"
provision "agent:codex"         "8250DF" "Operator request for Codex before provider assignment"
provision "provider:claude"     "1D76DB" "Durable system assignment to Claude"
provision "provider:codex"      "1D76DB" "Durable system assignment to Codex"
