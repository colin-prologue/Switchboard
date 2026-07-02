#!/usr/bin/env bash
# after_create.sh — workspace population (core spec §9.3/§9.4).
# Runs once, when the orchestrator has just created a brand-new per-issue
# workspace. cwd == the workspace directory (empty). Fatal on failure: the
# orchestrator aborts workspace creation, and MAY remove the partial dir.
#
# Env (exported by scripts/run-project.sh from the project binding):
#   SB_GITHUB_REPO   owner/name of the project repo
#   SB_BASE_BRANCH   base branch to clone
# Credentials come from `gh auth setup-git` — no token is written anywhere.
set -euo pipefail

: "${SB_GITHUB_REPO:?SB_GITHUB_REPO not set (run via scripts/run-project.sh)}"
: "${SB_BASE_BRANCH:?SB_BASE_BRANCH not set (run via scripts/run-project.sh)}"

echo "[after_create] cloning $SB_GITHUB_REPO (base: $SB_BASE_BRANCH) into $PWD"
git clone --branch "$SB_BASE_BRANCH" "https://github.com/$SB_GITHUB_REPO" .
echo "[after_create] clone complete: $(git rev-parse --short HEAD)"
