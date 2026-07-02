# AgDR-004: Agent permission posture (acceptEdits + git/gh + containment guard)

- **Status:** accepted (autonomous run, 2026-07-01)
- **Context:** Core §10.5 makes approval/sandbox posture implementation-defined
  but REQUIRES documenting it and never stalling on user input. SPEC.md §1
  binds "sandbox/safety invariants" to PreToolUse hooks vetoing tool calls
  outside the per-issue workspace.
- **Decision (documented posture):**
  1. `--permission-mode acceptEdits` + `--allowedTools "Bash(git:*)" "Bash(gh:*)"`
     in the workflow's claude.command. Anything else falls to the
     non-interactive default; a denial fails the attempt
     (user-input-required = hard failure, per the binding table).
  2. runner.py injects a PreToolUse containment guard (orchestrator/src/
     orchestrator/guard.py) via `--settings`: file-mutation tools
     (Write/Edit/NotebookEdit) targeting paths outside the workspace are
     denied with exit 2.
  3. The settings file lives NEXT TO the workspace (sibling path), never
     inside it, so the clone stays clean and the agent can't `git add` it.
- **v1 scope limit (explicit):** Bash commands are not statically analyzed —
  robust shell-path analysis is rabbit-hole territory. Blast radius there is
  bounded by workspace cwd, the fresh clone, and the git/gh allowlist.
- **Weakest point:** the guard trusts `CLAUDE_PROJECT_DIR`; an agent could in
  principle bypass via Bash file writes (`echo > /path`). Accepted v1 risk,
  documented here; tighten post-dogfood if the corpus shows attempts.
