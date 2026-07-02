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

echo "[before_run] ready on $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
