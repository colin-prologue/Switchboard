#!/usr/bin/env bash
# Self-host pilot launcher (issue #107): run ONE supervised mixed-provider
# checkpoint against colin-prologue/Switchboard itself, using the separate
# pilot workflow. The production Claude-only binding is untouched and is the
# rollback: `scripts/run-project.sh switchboard-self`.
#
# Usage: scripts/run-self-pilot-checkpoint.sh <explicit-codex|rollback> [--dry-run]
#   explicit-codex  file + dispatch the one agent:codex pilot issue (docs-only)
#   rollback        demonstrate rollback: print/exec the unchanged Claude-only
#                   launch after asserting no pilot orchestrator is running
#
# A second, weighted-route checkpoint is DELIBERATELY absent: per issue #107 it
# is separately gated on this checkpoint passing and is not part of the first
# authorization.
set -euo pipefail

SB_HOME="${SB_HOME:-$HOME/Developer/Switchboard}"
REPO="colin-prologue/Switchboard"
PROJECT_ENV="$SB_HOME/projects/switchboard-self/project.env"
PILOT_WORKFLOW="$SB_HOME/projects/switchboard-self/WORKFLOW.pilot-codex.md"
CHECKPOINT_DIR="$SB_HOME/projects/switchboard-self/pilot-checkpoints"
APP_ENV="$HOME/.config/switchboard/app.env"
# Prerequisite ISSUES (the #107 blockedBy set + the circuit ground-truth fix)
PREREQ_ISSUES="47 57 61 109"
PHASE="${1:-}"
DRY_RUN=0

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Reject anything that is not exactly --dry-run BEFORE any live action: a
# mistyped flag must fail loudly, never silently launch against the live
# repository (PR #117 review).
if [ "$#" -gt 2 ]; then
  fail "too many arguments; usage: $0 <explicit-codex|rollback> [--dry-run]"
fi
if [ -n "${2:-}" ]; then
  case "$2" in
    --dry-run) DRY_RUN=1 ;;
    *) fail "unknown argument: $2 (only --dry-run is accepted)" ;;
  esac
fi

case "$PHASE" in
  explicit-codex)
    TITLE="Self-pilot checkpoint 1: explicit Codex on Switchboard (docs-only)"
    BODY_FILE="$CHECKPOINT_DIR/01-explicit-codex.md"
    ISSUE_LABELS="status:todo,gate:triage-passed,agent:codex"
    ;;
  rollback)
    printf 'Rollback = the unchanged production Claude-only binding:\n'
    printf '  scripts/run-project.sh switchboard-self\n'
    PILOT_PIDS="$(pgrep -f "python -m orchestrator .*WORKFLOW.pilot-codex" || true)"
    [ -z "$PILOT_PIDS" ] || fail "pilot orchestrator still running (pid $PILOT_PIDS); stop it first (Ctrl-C or kill)"
    if [ "$DRY_RUN" -eq 1 ]; then
      printf 'RESULT: DRY RUN - rollback command printed, not executed.\n'
      exit 0
    fi
    printf 'Launching the production binding now (Ctrl-C to stop)...\n'
    exec "$SB_HOME/scripts/run-project.sh" switchboard-self
    ;;
  *)
    printf 'usage: %s <explicit-codex|rollback> [--dry-run]\n' "$0" >&2
    exit 2
    ;;
esac

printf 'phase: %s\nrepository: %s\nworkflow: %s\nissue labels: %s\n' \
  "$PHASE" "$REPO" "$PILOT_WORKFLOW" "$ISSUE_LABELS"

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'RESULT: DRY RUN - no GitHub writes and no process launch.\n'
  exit 0
fi

gh_clean() { env -u GITHUB_TOKEN -u GH_TOKEN gh "$@"; }

# --- preflight (issue #107 AC: verify before dispatch) ------------------------
for command in git gh uv python3; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is not installed"
done
if ! command -v codex >/dev/null 2>&1; then
  BUNDLED_CODEX="/Applications/ChatGPT.app/Contents/Resources/codex"
  [ -x "$BUNDLED_CODEX" ] || fail "Codex CLI not found on PATH or at $BUNDLED_CODEX"
  export PATH="$(dirname "$BUNDLED_CODEX"):$PATH"
fi
codex login status >/dev/null 2>&1 || fail "Codex subscription login is not ready"
command -v claude >/dev/null 2>&1 || fail "Claude CLI not found on PATH"
gh_clean auth status >/dev/null 2>&1 || fail "gh is not authenticated"

[ -d "$SB_HOME/.git" ] || fail "Switchboard checkout not found: $SB_HOME"
[ -f "$APP_ENV" ] || fail "GitHub App environment not found: $APP_ENV"
[ -f "$PROJECT_ENV" ] || fail "self project binding not found: $PROJECT_ENV"
[ -f "$PILOT_WORKFLOW" ] || fail "pilot workflow not found: $PILOT_WORKFLOW"
[ -f "$BODY_FILE" ] || fail "checkpoint issue body not found: $BODY_FILE"

[ "$(git -C "$SB_HOME" branch --show-current)" = "main" ] \
  || fail "checkout must be on main (pilot config is reviewed at HEAD)"
[ -z "$(git -C "$SB_HOME" status --porcelain)" ] \
  || fail "checkout has uncommitted changes"
git -C "$SB_HOME" pull --ff-only origin main
[ -z "$(git -C "$SB_HOME" status --porcelain)" ] \
  || fail "checkout changed during update"

pgrep -f "python -m orchestrator" >/dev/null \
  && fail "an orchestrator is already running against this host; stop it first"

unset GITHUB_TOKEN GH_TOKEN
set -a
# shellcheck source=/dev/null
. "$APP_ENV"
# shellcheck source=/dev/null
. "$PROJECT_ENV"
set +a
export SB_HOME
for key in SB_APP_ID SB_APP_INSTALLATION_ID SB_APP_PRIVATE_KEY_FILE \
           SB_APP_BOT_LOGIN SB_APP_BOT_USER_ID; do
  [ -n "${!key:-}" ] || fail "$key is not set by $APP_ENV"
done
[ "$SB_GITHUB_REPO" = "$REPO" ] \
  || fail "project binding targets $SB_GITHUB_REPO instead of $REPO"

PROVISIONED_LABELS="$(gh_clean label list --repo "$REPO" --limit 100 \
  --json name --jq '.[].name')"
for label in status:todo status:in-progress status:human-review status:parked \
             gate:triage-passed agent:codex provider:claude provider:codex; do
  printf '%s\n' "$PROVISIONED_LABELS" | grep -qxF "$label" \
    || fail "required label is not provisioned: $label (gh label create '$label' --repo $REPO)"
done

for issue in $PREREQ_ISSUES; do
  STATE="$(gh_clean issue view "$issue" --repo "$REPO" --json state --jq .state)"
  [ "$STATE" = "CLOSED" ] || fail "prerequisite issue #$issue is $STATE, not CLOSED"
done

CURRENT_COUNT="$(CHECKPOINT_TITLE="$TITLE" gh_clean issue list --repo "$REPO" \
  --state all --limit 100 --json title --jq \
  '[.[] | select(.title == env.CHECKPOINT_TITLE)] | length')"
[ "$CURRENT_COUNT" = "0" ] || fail "checkpoint already exists: $TITLE"

CODEX_LABELED="$(gh_clean issue list --repo "$REPO" --state open \
  --label agent:codex --json number --jq 'length')"
[ "$CODEX_LABELED" = "0" ] \
  || fail "an open agent:codex issue already exists; the pilot issue must be the only one"

# --- file the pilot issue and launch -----------------------------------------
ISSUE_URL="$(gh_clean issue create --repo "$REPO" --title "$TITLE" \
  --body-file "$BODY_FILE" --label "$ISSUE_LABELS")"
ISSUE_NUMBER="${ISSUE_URL##*/}"
BRANCH="switchboard/issue-$ISSUE_NUMBER"
OPEN_PRS_ON_BRANCH="$(gh_clean pr list --repo "$REPO" --state open \
  --head "$BRANCH" --json number --jq 'length')"
[ "$OPEN_PRS_ON_BRANCH" = "0" ] \
  || fail "conflicting open PR already exists for $BRANCH"

TEMP_DIR="$(mktemp -d "/private/tmp/switchboard-self-pilot.XXXXXX")"
LOG="$TEMP_DIR/orchestrator-$(date -u +%Y%m%dT%H%M%SZ).log"
printf 'issue: %s\nlog: %s\n' "$ISSUE_URL" "$LOG"

(
  cd "$SB_HOME"
  exec uv run --project orchestrator python -m orchestrator \
    --provider mixed --workflow "$PILOT_WORKFLOW"
) >"$LOG" 2>&1 &
ORCHESTRATOR_PID=$!
cleanup() {
  if kill -0 "$ORCHESTRATOR_PID" 2>/dev/null; then
    kill -TERM "$ORCHESTRATOR_PID" 2>/dev/null || true
  fi
  wait "$ORCHESTRATOR_PID" 2>/dev/null || true
}
trap cleanup EXIT

# --- supervise: durable assignment, then verified handoff ---------------------
deadline=$((SECONDS + 2700))
STOP_STATUS=""
while [ "$SECONDS" -lt "$deadline" ]; do
  kill -0 "$ORCHESTRATOR_PID" 2>/dev/null || fail "orchestrator exited early; see $LOG"
  LABELS="$(gh_clean issue view "$ISSUE_NUMBER" --repo "$REPO" \
    --json labels --jq '[.labels[].name] | join(",")')"
  case "$LABELS" in
    *status:human-review*) STOP_STATUS="human-review"; break ;;
    *status:parked*)       STOP_STATUS="parked"; break ;;
  esac
  sleep 20
done
[ "$STOP_STATUS" = "human-review" ] \
  || fail "pilot did not reach human-review (last state: ${STOP_STATUS:-timeout}); see $LOG"

# Durable-assignment-before-claim is verified from ORCHESTRATOR LOG ORDER, not
# label polling (PR #117 review: a fast dispatch outruns the first poll sample,
# so observation order proves nothing). The persisted-assignment line must
# precede the claim-visibility swap line in the log stream.
ASSIGN_LINE="$(grep -n "mixed provider assignment persisted" "$LOG" | head -1 | cut -d: -f1)"
CLAIM_LINE="$(grep -nE "dispatched|in-progress label" "$LOG" | head -1 | cut -d: -f1)"
[ -n "$ASSIGN_LINE" ] || fail "log does not show a persisted provider assignment"
[ -n "$CLAIM_LINE" ] || fail "log does not show the claim/dispatch"
[ "$ASSIGN_LINE" -lt "$CLAIM_LINE" ] \
  || fail "provider assignment (log line $ASSIGN_LINE) did not precede the claim (line $CLAIM_LINE)"
FINAL_LABELS="$(gh_clean issue view "$ISSUE_NUMBER" --repo "$REPO" \
  --json labels --jq '[.labels[].name] | join(",")')"
case "$FINAL_LABELS" in *provider:codex*) ;; *)
  fail "durable provider:codex label is missing at handoff" ;;
esac

# --- evidence assertions (issue #107 AC) --------------------------------------
WORKSPACE="$SB_WORKSPACE_ROOT/$ISSUE_NUMBER"
[ -f "$WORKSPACE/.run/handoff-evidence.json" ] \
  || fail "production handoff evidence file is missing"
grep -q "handoff evidence validated; issue moved to human-review" "$LOG" \
  || fail "log does not prove the orchestrator-owned handoff transition"
grep -q "provider_id=codex" "$LOG" \
  || fail "log does not prove Codex executed the turn"
PR_COUNT="$(gh_clean pr list --repo "$REPO" --state open --head "$BRANCH" \
  --json number --jq 'length')"
[ "$PR_COUNT" = "1" ] || fail "expected one open handoff PR on $BRANCH, found $PR_COUNT"
[ -z "$(git -C "$WORKSPACE" status --porcelain | grep -v '^.. \.run/' || true)" ] \
  || fail "workspace is not clean after handoff"

RECORD="$CHECKPOINT_DIR/RESULT-01-explicit-codex.md"
{
  printf '# Self-pilot checkpoint 1 result (%s)\n\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf -- '- issue: %s\n- branch: %s\n- workspace: %s\n- log: %s\n' \
    "$ISSUE_URL" "$BRANCH" "$WORKSPACE" "$LOG"
  printf -- '- evidence: %s/.run/handoff-evidence.json\n' "$WORKSPACE"
  printf -- '- pr: %s\n' "$(gh_clean pr list --repo "$REPO" --state open \
      --head "$BRANCH" --json url --jq '.[0].url')"
} > "$RECORD"
printf 'checkpoint record: %s\n' "$RECORD"
printf 'STOP: review and merge the handoff PR, confirm the issue closes, then\n'
printf 'commit the checkpoint record. Rollback demonstration:\n'
printf '  scripts/run-self-pilot-checkpoint.sh rollback\n'
