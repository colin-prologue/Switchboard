#!/usr/bin/env bash
# Run exactly one reviewed Stage 6 mixed-canary evidence checkpoint.
# Usage: scripts/run-mixed-canary-checkpoint.sh <phase> [--dry-run]
# Phases: explicit-claude, explicit-codex, weighted-claude, rollback-claude
set -euo pipefail

SB_HOME="${SB_HOME:-$HOME/Developer/Switchboard}"
REPO="colin-prologue/switchboard-mixed-canary"
PROJECT_ENV="$SB_HOME/projects/mixed-canary/project.env"
MIXED_WORKFLOW="$SB_HOME/projects/mixed-canary/WORKFLOW.md"
ROLLBACK_WORKFLOW="$SB_HOME/projects/mixed-canary/WORKFLOW.rollback-claude.md"
CHECKPOINT_DIR="$SB_HOME/projects/mixed-canary/checkpoints"
APP_ENV="$HOME/.config/switchboard/app.env"
PHASE="${1:-}"
DRY_RUN=0

if [ "${2:-}" = "--dry-run" ]; then
  DRY_RUN=1
elif [ -n "${2:-}" ]; then
  printf 'ERROR: unknown argument: %s\n' "$2" >&2
  exit 2
fi

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

case "$PHASE" in
  explicit-claude)
    TITLE="Canary checkpoint 1: explicit Claude assignment"
    BODY_FILE="$CHECKPOINT_DIR/01-explicit-claude.md"
    ISSUE_LABELS="status:todo,gate:triage-passed,agent:claude"
    EXPECTED_PROVIDER="claude"
    EXPECTED_DURABLE_PROVIDER="claude"
    RUN_MODE="mixed"
    PREREQUISITES=""
    ;;
  explicit-codex)
    TITLE="Canary checkpoint 2: explicit Codex assignment"
    BODY_FILE="$CHECKPOINT_DIR/02-explicit-codex.md"
    ISSUE_LABELS="status:todo,gate:triage-passed,agent:codex"
    EXPECTED_PROVIDER="codex"
    EXPECTED_DURABLE_PROVIDER="codex"
    RUN_MODE="mixed"
    PREREQUISITES="Canary checkpoint 1: explicit Claude assignment"
    ;;
  weighted-claude)
    TITLE="Canary checkpoint 3: zero-weight Codex routes to Claude"
    BODY_FILE="$CHECKPOINT_DIR/03-weighted-claude.md"
    ISSUE_LABELS="status:todo,gate:triage-passed"
    EXPECTED_PROVIDER="claude"
    EXPECTED_DURABLE_PROVIDER="claude"
    RUN_MODE="mixed"
    PREREQUISITES="Canary checkpoint 1: explicit Claude assignment
Canary checkpoint 2: explicit Codex assignment"
    ;;
  rollback-claude)
    TITLE="Canary checkpoint 4: Claude-only rollback ignores mixed assignment"
    BODY_FILE="$CHECKPOINT_DIR/04-rollback-claude.md"
    ISSUE_LABELS="status:todo,gate:triage-passed,provider:codex"
    EXPECTED_PROVIDER="claude"
    EXPECTED_DURABLE_PROVIDER="codex"
    RUN_MODE="default-claude"
    PREREQUISITES="Canary checkpoint 1: explicit Claude assignment
Canary checkpoint 2: explicit Codex assignment
Canary checkpoint 3: zero-weight Codex routes to Claude"
    ;;
  *)
    printf 'usage: %s <%s> [--dry-run]\n' "$0" \
      "explicit-claude|explicit-codex|weighted-claude|rollback-claude" >&2
    exit 2
    ;;
esac

if [ "$RUN_MODE" = "mixed" ]; then
  WORKFLOW="$MIXED_WORKFLOW"
  CLI_PROVIDER="mixed"
else
  WORKFLOW="$ROLLBACK_WORKFLOW"
  CLI_PROVIDER="default (flag omitted)"
fi

printf 'phase: %s\n' "$PHASE"
printf 'title: %s\n' "$TITLE"
printf 'repository: %s\n' "$REPO"
printf 'workflow: %s\n' "$WORKFLOW"
printf 'cli provider: %s\n' "$CLI_PROVIDER"
printf 'issue labels: %s\n' "$ISSUE_LABELS"
printf 'expected dispatch provider: %s\n' "$EXPECTED_PROVIDER"
printf 'expected durable provider label: %s\n' "$EXPECTED_DURABLE_PROVIDER"
printf 'prerequisites: %s\n' "${PREREQUISITES:-none}"

if [ "$DRY_RUN" -eq 1 ]; then
  printf 'RESULT: DRY RUN - no GitHub writes and no process launch.\n'
  exit 0
fi

gh_clean() {
  env -u GITHUB_TOKEN -u GH_TOKEN gh "$@"
}

for command in git gh uv python3; do
  command -v "$command" >/dev/null 2>&1 || fail "$command is not installed"
done
[ -d "$SB_HOME/.git" ] || fail "primary Switchboard checkout not found: $SB_HOME"
[ -f "$APP_ENV" ] || fail "GitHub App environment not found: $APP_ENV"
[ -f "$PROJECT_ENV" ] || fail "mixed-canary project binding not found: $PROJECT_ENV"
[ -f "$WORKFLOW" ] || fail "checkpoint workflow not found: $WORKFLOW"
[ -f "$BODY_FILE" ] || fail "checkpoint issue body not found: $BODY_FILE"

if [ "$EXPECTED_PROVIDER" = "codex" ]; then
  if ! command -v codex >/dev/null 2>&1; then
    BUNDLED_CODEX="/Applications/ChatGPT.app/Contents/Resources/codex"
    [ -x "$BUNDLED_CODEX" ] || fail "Codex CLI not found on PATH or at $BUNDLED_CODEX"
    export PATH="$(dirname "$BUNDLED_CODEX"):$PATH"
  fi
  codex login status >/dev/null 2>&1 || fail "Codex subscription login is not ready"
else
  command -v claude >/dev/null 2>&1 || fail "Claude CLI not found on PATH"
fi

gh_clean auth status >/dev/null 2>&1 || fail "gh is not authenticated"
[ "$(git -C "$SB_HOME" branch --show-current)" = "main" ] \
  || fail "primary Switchboard checkout must be on main"
[ -z "$(git -C "$SB_HOME" status --porcelain)" ] \
  || fail "primary Switchboard checkout has uncommitted changes"
git -C "$SB_HOME" pull --ff-only origin main
[ -z "$(git -C "$SB_HOME" status --porcelain)" ] \
  || fail "primary Switchboard checkout changed during update"

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
[ "$(gh_clean repo view "$REPO" --json isPrivate --jq .isPrivate)" = "true" ] \
  || fail "$REPO must remain private"

PROVISIONED_LABELS="$(gh_clean label list --repo "$REPO" --limit 100 \
  --json name --jq '.[].name')"
for label in status:todo status:in-progress status:human-review status:parked \
             status:blocked gate:triage-passed agent:claude agent:codex \
             provider:claude provider:codex; do
  printf '%s\n' "$PROVISIONED_LABELS" | grep -qxF "$label" \
    || fail "required label is not provisioned: $label"
done

OPEN_ISSUES="$(gh_clean issue list --repo "$REPO" --state open --limit 100 \
  --json number --jq 'length')"
OPEN_PRS="$(gh_clean pr list --repo "$REPO" --state open --limit 100 \
  --json number --jq 'length')"
[ "$OPEN_ISSUES" = "0" ] \
  || fail "$REPO has $OPEN_ISSUES open issue(s); finish the current checkpoint first"
[ "$OPEN_PRS" = "0" ] \
  || fail "$REPO has $OPEN_PRS open pull request(s); finish the current checkpoint first"

CURRENT_COUNT="$(CHECKPOINT_TITLE="$TITLE" gh_clean issue list --repo "$REPO" \
  --state all --limit 100 --json title --jq \
  '[.[] | select(.title == env.CHECKPOINT_TITLE)] | length')"
[ "$CURRENT_COUNT" = "0" ] || fail "checkpoint already exists: $TITLE"

while IFS= read -r prerequisite; do
  [ -n "$prerequisite" ] || continue
  CLOSED_COUNT="$(CHECKPOINT_TITLE="$prerequisite" gh_clean issue list \
    --repo "$REPO" --state closed --limit 100 --json title --jq \
    '[.[] | select(.title == env.CHECKPOINT_TITLE)] | length')"
  [ "$CLOSED_COUNT" = "1" ] \
    || fail "prerequisite checkpoint is not closed exactly once: $prerequisite"
done <<EOF
$PREREQUISITES
EOF

ISSUE_URL="$(gh_clean issue create --repo "$REPO" --title "$TITLE" \
  --body-file "$BODY_FILE" --label "$ISSUE_LABELS")"
ISSUE_NUMBER="${ISSUE_URL##*/}"
WORKSPACE="$SB_WORKSPACE_ROOT/$ISSUE_NUMBER"
[ ! -e "$WORKSPACE" ] || fail "new issue workspace already exists: $WORKSPACE"

TEMP_DIR="$(mktemp -d "/private/tmp/switchboard-mixed-$PHASE.XXXXXX")"
LOG="$TEMP_DIR/orchestrator-$(date -u +%Y%m%dT%H%M%SZ).log"
printf 'issue: %s\n' "$ISSUE_URL"
printf 'log: %s\n' "$LOG"

if [ "$RUN_MODE" = "mixed" ]; then
  (
    cd "$SB_HOME"
    exec uv run --project orchestrator python -m orchestrator \
      --provider mixed --workflow "$WORKFLOW"
  ) >"$LOG" 2>&1 &
else
  (
    cd "$SB_HOME"
    exec uv run --project orchestrator python -m orchestrator \
      --workflow "$WORKFLOW"
  ) >"$LOG" 2>&1 &
fi
ORCHESTRATOR_PID=$!

cleanup() {
  if kill -0 "$ORCHESTRATOR_PID" 2>/dev/null; then
    kill -TERM "$ORCHESTRATOR_PID" 2>/dev/null || true
  fi
  wait "$ORCHESTRATOR_PID" 2>/dev/null || true
}
trap cleanup EXIT

deadline=$((SECONDS + 1800))
STOP_STATUS=""
FINAL_LABELS=""
LAST_LABELS=""
while [ "$SECONDS" -lt "$deadline" ]; do
  if FINAL_LABELS="$(gh_clean issue view "$ISSUE_NUMBER" --repo "$REPO" \
      --json labels --jq '.labels | map(.name) | join(",")' 2>/dev/null)"; then
    if [ "$FINAL_LABELS" != "$LAST_LABELS" ]; then
      printf 'labels: %s\n' "$FINAL_LABELS"
      LAST_LABELS="$FINAL_LABELS"
    fi
    case ",$FINAL_LABELS," in
      *,status:human-review,*) STOP_STATUS="human-review"; break ;;
      *,status:parked,*) STOP_STATUS="parked"; break ;;
      *,status:blocked,*) STOP_STATUS="blocked"; break ;;
      *,status:drafting,*) STOP_STATUS="drafting"; break ;;
      *,status:plan-review,*) STOP_STATUS="plan-review"; break ;;
    esac
  fi
  if ! kill -0 "$ORCHESTRATOR_PID" 2>/dev/null; then
    tail -160 "$LOG" >&2 || true
    fail "orchestrator exited before a named checkpoint stop condition"
  fi
  sleep 2
done

cleanup
trap - EXIT

[ -n "$STOP_STATUS" ] || { tail -160 "$LOG" >&2 || true; fail "timed out after 30 minutes"; }
[ "$STOP_STATUS" = "human-review" ] \
  || { tail -160 "$LOG" >&2 || true; fail "checkpoint stopped at status:$STOP_STATUS"; }

case ",$FINAL_LABELS," in
  *,provider:$EXPECTED_DURABLE_PROVIDER,*) ;;
  *) fail "missing expected provider:$EXPECTED_DURABLE_PROVIDER label (labels: $FINAL_LABELS)" ;;
esac
if [ "$PHASE" = "weighted-claude" ]; then
  case ",$FINAL_LABELS," in
    *,agent:claude,*|*,agent:codex,*) fail "weighted checkpoint acquired an agent override" ;;
  esac
fi
if [ "$PHASE" = "rollback-claude" ]; then
  case ",$FINAL_LABELS," in
    *,provider:claude,*) fail "Claude-only rollback rewrote the durable Codex label" ;;
  esac
fi

grep -q "dispatched .*issue_identifier=$ISSUE_NUMBER .*provider_id=$EXPECTED_PROVIDER" "$LOG" \
  || fail "log does not prove provider_id=$EXPECTED_PROVIDER dispatch"
[ -d "$WORKSPACE/.git" ] || fail "workspace was not created: $WORKSPACE"
(
  cd "$WORKSPACE"
  python3 -m unittest discover -s tests -v
)
[ -z "$(git -C "$WORKSPACE" status --porcelain)" ] \
  || fail "workspace is not clean after handoff"
PR_COUNT="$(gh_clean pr list --repo "$REPO" --state open \
  --head "switchboard/issue-$ISSUE_NUMBER" --json number --jq 'length')"
[ "$PR_COUNT" = "1" ] || fail "expected one open handoff PR, found $PR_COUNT"

if [ "$EXPECTED_PROVIDER" = "codex" ]; then
  TRANSCRIPTS="$(find "$WORKSPACE/.run/transcripts" -type f \
    -name 'codex-*.jsonl' 2>/dev/null | wc -l | tr -d '[:space:]')"
  [ "$TRANSCRIPTS" -ge 1 ] || fail "Codex checkpoint has no raw JSONL transcript"
fi

printf '\nRESULT: PASS - %s reached human review via provider_id=%s.\n' \
  "$PHASE" "$EXPECTED_PROVIDER"
printf 'Issue: %s\n' "$ISSUE_URL"
printf 'Labels: %s\n' "$FINAL_LABELS"
printf 'Log: %s\n' "$LOG"
printf 'Workspace: %s\n' "$WORKSPACE"
printf 'STOP: review and merge the handoff PR, confirm the issue closes, then return here.\n'
