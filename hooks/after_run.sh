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

# Ground-truth transcript capture (issue #30, blocks #20b / #16).
# Copy this session's CLI transcript(s) into the workspace's .run/transcripts/
# so a fresh fail-review verifier reads them FROM DISK (ADR-013 inversion)
# instead of trusting returned summaries. Transcripts carry secrets: .run/ is
# gitignored and their content is NEVER committed or posted to GitHub — this
# writes to local disk only.
#
# The project-dir name is derivable from the workspace path (safety invariant:
# cwd == the per-issue workspace), so no session id is needed. Claude Code
# encodes the absolute cwd by replacing every "/" and "." with "-".
{
  transcript_src="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/$(printf '%s' "$PWD" | sed 's#[/.]#-#g')"
  if [ -d "$transcript_src" ]; then
    mkdir -p .run/transcripts
    # Overwrite-safe on a reused workspace; no-op if the glob matches nothing.
    cp -f "$transcript_src"/*.jsonl .run/transcripts/ 2>/dev/null || true
  fi
} || true

exit 0
