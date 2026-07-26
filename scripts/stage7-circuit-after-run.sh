#!/usr/bin/env bash
# Stage 7 circuit-canary handoff boundary.
#
# The recovery agent may prepare a handoff while its Codex process is still
# active, but it must not make its own issue non-dispatchable. The agent writes
# a git-excluded ready marker instead. This hook runs after the provider process
# exits, retains the normal artifact capture, and moves the synthetic issue to
# human review only when the latest raw Codex transcript proves natural turn
# completion and the pushed PR/workspace invariants all hold.
set -euo pipefail

BASE_AFTER_RUN="${SWITCHBOARD_CANARY_BASE_AFTER_RUN:-$SB_HOME/hooks/after_run.sh}"
GH_BIN="${SWITCHBOARD_CANARY_GH_BIN:-gh}"
READY_MARKER=".run/stage7-handoff-ready"
TRANSCRIPT_DIR=".run/transcripts"
EXPECTED_REPO="colin-prologue/switchboard-mixed-canary"

"$BASE_AFTER_RUN"

[ -f "$READY_MARKER" ] || exit 0

: "${SB_GITHUB_REPO:?SB_GITHUB_REPO not set}"
[ "$SB_GITHUB_REPO" = "$EXPECTED_REPO" ] || {
  printf 'ERROR: Stage 7 handoff targets %s instead of %s\n' \
    "$SB_GITHUB_REPO" "$EXPECTED_REPO" >&2
  exit 1
}

ISSUE_NUMBER="$(basename "$PWD")"
BRANCH="switchboard/issue-$ISSUE_NUMBER"

# A cancelled Codex stream has no terminal turn.completed record. Leave the
# marker and issue state unchanged so a killed/failed run can never look like a
# successful handoff.
LATEST_TRANSCRIPT="$(
  find "$TRANSCRIPT_DIR" -maxdepth 1 -type f -name 'codex-*.jsonl' \
    -print 2>/dev/null | sort | tail -n 1
)"
if [ -z "$LATEST_TRANSCRIPT" ] \
  || ! tail -n 1 "$LATEST_TRANSCRIPT" \
    | grep -Eq '"type"[[:space:]]*:[[:space:]]*"turn\.completed"'; then
  printf '[stage7-after-run] handoff marker retained: latest Codex turn is incomplete\n'
  exit 0
fi

[ "$(git branch --show-current)" = "$BRANCH" ] || {
  printf 'ERROR: Stage 7 handoff is on the wrong branch\n' >&2
  exit 1
}
[ -z "$(git status --porcelain)" ] || {
  printf 'ERROR: Stage 7 handoff workspace is dirty\n' >&2
  exit 1
}
git show-ref --verify --quiet "refs/remotes/origin/$BRANCH" || {
  printf 'ERROR: Stage 7 handoff branch is not pushed\n' >&2
  exit 1
}
[ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$BRANCH")" ] || {
  printf 'ERROR: Stage 7 handoff commit is not pushed\n' >&2
  exit 1
}

gh_clean() {
  env -u GITHUB_TOKEN -u GH_TOKEN "$GH_BIN" "$@"
}

PR_COUNT="$(
  gh_clean pr list --repo "$SB_GITHUB_REPO" --state open --head "$BRANCH" \
    --json number --jq length
)"
[ "$PR_COUNT" = "1" ] || {
  printf 'ERROR: Stage 7 handoff expected one open PR, found %s\n' \
    "$PR_COUNT" >&2
  exit 1
}
PR_BODY="$(
  gh_clean pr list --repo "$SB_GITHUB_REPO" --state open --head "$BRANCH" \
    --json body --jq '.[0].body'
)"
printf '%s\n' "$PR_BODY" \
  | grep -Eiq "(close[sd]?|fix(e[sd])?|resolve[sd]?) +#$ISSUE_NUMBER([^0-9]|$)" \
  || {
    printf 'ERROR: Stage 7 handoff PR body does not close issue #%s\n' \
      "$ISSUE_NUMBER" >&2
    exit 1
  }

gh_clean issue edit "$ISSUE_NUMBER" --repo "$SB_GITHUB_REPO" \
  --add-label "status:human-review" --remove-label "status:in-progress"
rm -f "$READY_MARKER"
printf '[stage7-after-run] issue #%s moved to human review after terminal Codex success\n' \
  "$ISSUE_NUMBER"
