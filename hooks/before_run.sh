#!/usr/bin/env bash
# before_run.sh — ensure the per-issue branch (core spec §9.4).
# Runs before every run attempt. cwd == the per-issue workspace (a clone made
# by after_create.sh). Fatal on failure: the current attempt errors out.
#
# The issue number is derived from the workspace directory name (the orchestrator
# names workspaces by sanitized issue identifier, core spec §9.1).
#
# First run:  create switchboard/issue-<n> from up-to-date origin/<base>.
# Reused workspace: check the branch out; do NOT destructively reset (§9.3).
set -euo pipefail

: "${SB_BASE_BRANCH:?SB_BASE_BRANCH not set (run via scripts/run-project.sh)}"

ISSUE="$(basename "$PWD")"
BRANCH="switchboard/issue-$ISSUE"
REMOTE_REF="origin/$SB_BASE_BRANCH"
STALENESS_FILE="$PWD/.sb-staleness"

# .sb-staleness is workspace-local surfacing (issue #57): agents read it on
# turn 1, but it must never land in a PR. Exclude it in the clone's own
# .git/info/exclude (a per-clone ignore that isn't itself committed).
ensure_staleness_excluded() {
  local exclude=".git/info/exclude"
  [ -f "$exclude" ] || return 0
  grep -qxF ".sb-staleness" "$exclude" || echo ".sb-staleness" >> "$exclude"
}

# write_staleness <reason> — record why the ff-update was skipped/refused so the
# dispatched session can see it was built against a non-fresh HEAD. behind-count
# is derived, never hard-coded (matches the test's own derivation).
write_staleness() {
  local reason="$1" behind
  behind="$(git rev-list --count "HEAD..$REMOTE_REF")"
  {
    echo "reason=$reason"
    echo "behind-count=$behind"
    echo "workspace-head=$(git rev-parse HEAD)"
    echo "origin-head=$(git rev-parse "$REMOTE_REF")"
  } > "$STALENESS_FILE"
  echo "[before_run] STALE reason=$reason behind=$behind (see .sb-staleness)"
}

git fetch origin "$SB_BASE_BRANCH"

ensure_staleness_excluded

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "[before_run] reusing existing branch $BRANCH"
  git checkout "$BRANCH"

  # Freshness (issue #57 / §9.3): a reused branch that never commits (e.g. a
  # triage verifier) stays pinned at origin-of-first-clone unless we ff it to
  # the freshly-fetched origin/base. The prechecks below — NOT ff-only's exit
  # status — are what keep HEAD from moving under local state: ff-only succeeds
  # on a dirty tree whose edits don't overlap the update, and its guard is
  # per-file, so it is not a safe dirty/divergence detector on its own.
  if [ -n "$(git status --porcelain)" ]; then
    # Dirty gate: local edits present ⇒ never merge. HEAD stays put (§9.3).
    write_staleness dirty
  elif git merge-base --is-ancestor "$REMOTE_REF" HEAD; then
    # origin/base is an ancestor of HEAD ⇒ fresh or ahead-only. Not stale.
    rm -f "$STALENESS_FILE"
  elif git merge-base --is-ancestor HEAD "$REMOTE_REF"; then
    # HEAD is an ancestor of origin/base ⇒ behind-only, clean ⇒ guaranteed ff.
    git merge --ff-only "$REMOTE_REF"
    rm -f "$STALENESS_FILE"
  else
    # Neither is an ancestor ⇒ diverged. Refuse to move HEAD (§9.3).
    write_staleness diverged
  fi
else
  echo "[before_run] creating $BRANCH from origin/$SB_BASE_BRANCH"
  git checkout -B "$BRANCH" "origin/$SB_BASE_BRANCH"
fi

# issue #10: author workspace commits as the Switchboard bot and push with the
# per-turn installation token. Set only when the App bot identity is exported
# (run-project.sh sources ~/.config/switchboard/app.env); absent it, git falls
# back to the operator's identity (documented personal-token dogfood path).
if [ -n "${SB_APP_BOT_LOGIN:-}" ] && [ -n "${SB_APP_BOT_USER_ID:-}" ]; then
  git config user.name  "$SB_APP_BOT_LOGIN"
  git config user.email "${SB_APP_BOT_USER_ID}+${SB_APP_BOT_LOGIN}@users.noreply.github.com"
  # x-access-token is GitHub's username for installation-token HTTPS auth. The
  # single quotes are load-bearing: $GITHUB_TOKEN must resolve AT PUSH TIME in
  # the agent's env (the orchestrator injects a fresh mint per turn), never be
  # baked in here where it would go stale within the hour.
  git config credential.helper '!f() { echo "username=x-access-token"; echo "password=$GITHUB_TOKEN"; }; f'
  echo "[before_run] commit identity: $SB_APP_BOT_LOGIN"
fi

echo "[before_run] ready on $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
