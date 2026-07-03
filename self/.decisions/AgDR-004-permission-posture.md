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

## Addendum (2026-07-03, adversarial audit — reconcile record with shipped posture)

- **Denial semantics ratified as SOFT.** The original decision text ("a
  denial fails the attempt") never matched the implementation: denials are
  fed back to the agent, which may route around them; only a session that
  cannot finish (non-success terminal `result`) fails the attempt. This soft
  semantic is what ran and was validated when the allowlist fix (PR #13)
  unblocked issue #11 (PR #17). SPEC.md §1 now records the soft semantics;
  the hard-fail wording here is superseded. Rejected alternative
  (steelman): hard-failing on any `permission_denials` in the result would
  make the spec sentence true and surface permission-starved sessions
  faster — but it retries sessions that actually succeeded despite an
  incidental denial, and the parking cap already converts persistently
  denied sessions into a human checkpoint.
- **Allowlist drift folded in:** the live posture also allowlists two
  workspace-scoped pytest invocations
  (`Bash(uv run --project orchestrator python -m pytest:*)` and the
  `python -m pytest` variant) so workers can satisfy verification criteria
  (PR #13). Accepted risk, previously recorded only in a WORKFLOW.base.md
  comment: pytest executes repo code, so a worker-committed conftest/plugin
  runs outside the containment guard. Blast radius bounded by workspace cwd
  + fresh clone + the same allowlist.
- **Guard matcher superset:** the PreToolUse guard also matches `MultiEdit`
  (harmless superset of the Write/Edit/NotebookEdit list above).
- **Environment inheritance made explicit:** the agent subprocess inherits
  the orchestrator's environment including `GITHUB_TOKEN`; "the token never
  enters the workspace" means never written to disk in the clone. Bash-level
  env access is inside the v1 scope limit above. Scrubbing the env was
  rejected: workers' `gh` tooling (tracker writes, PR creation) requires the
  token by design.
