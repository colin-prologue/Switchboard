#!/usr/bin/env bash
# after_run.sh — post-run artifact capture (core spec §9.4).
# Runs after every attempt; failures here are logged and ignored by the
# orchestrator, so keep it cheap and non-essential.
# cwd == the per-issue workspace.
set -uo pipefail

# The log lives NEXT TO the workspace (like the guard settings), never inside
# the clone — an in-clone file shows up as dirty status the agent may commit.
{
  echo "=== after_run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
  git rev-parse --abbrev-ref HEAD 2>/dev/null
  git log --oneline -3 2>/dev/null
  git status --short 2>/dev/null
} >> "../.$(basename "$PWD").run.log" 2>&1 || true

exit 0
