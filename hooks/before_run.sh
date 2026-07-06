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

git fetch origin "$SB_BASE_BRANCH"

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  echo "[before_run] reusing existing branch $BRANCH"
  git checkout "$BRANCH"
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
