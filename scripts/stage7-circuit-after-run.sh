#!/usr/bin/env bash
# Stage 7 circuit-canary after_run hook — artifact capture passthrough.
#
# HISTORY (issue #61): this hook previously validated transcripts, PR
# invariants, and moved the issue to status:human-review itself — a
# canary-only parallel implementation of terminal handoff. That ownership now
# lives in the production orchestrator: the agent writes the evidence contract
# (.run/handoff-evidence.json — see orchestrator/src/orchestrator/handoff.py),
# and the orchestrator validates it and performs the single label transition
# after verified provider success. This hook retains only the base artifact
# capture plus the repo guard, so checkpoint coverage exercises the production
# mechanism rather than a parallel one.
set -euo pipefail

BASE_AFTER_RUN="${SWITCHBOARD_CANARY_BASE_AFTER_RUN:-$SB_HOME/hooks/after_run.sh}"
EXPECTED_REPO="colin-prologue/switchboard-mixed-canary"

"$BASE_AFTER_RUN"

: "${SB_GITHUB_REPO:?SB_GITHUB_REPO not set}"
[ "$SB_GITHUB_REPO" = "$EXPECTED_REPO" ] || {
  printf 'ERROR: Stage 7 canary hook ran against %s instead of %s\n' \
    "$SB_GITHUB_REPO" "$EXPECTED_REPO" >&2
  exit 1
}

if [ -f ".run/handoff-evidence.json" ]; then
  printf '[stage7-after-run] handoff evidence present; validation and the label transition are orchestrator-owned (issue #61)\n'
fi
