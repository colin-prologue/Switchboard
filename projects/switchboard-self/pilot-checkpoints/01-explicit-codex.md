## Intent

Self-host pilot checkpoint 1 (issue #107): one bounded, docs-only task
dispatched to an explicitly assigned Codex worker on the Switchboard repository
itself, under supervision. This issue is filed by
`scripts/run-self-pilot-checkpoint.sh` with `agent:codex` and is the ONLY issue
the pilot orchestrator can dispatch (its workflow pins
`required_labels: ["agent:codex", "gate:triage-passed"]`).

Task: the operator-facing README has no description of the worker handoff
evidence contract that now governs every handoff (issue #61 / AgDR-028).
Document it.

## Acceptance criteria

- [ ] `README.md` gains a "Worker handoff evidence" subsection (placement:
      alongside the existing operator/workflow documentation) covering: the
      `.run/handoff-evidence.json` fields (`issue`, `pr_number`, `head_sha`),
      that writing it is the worker's FINAL action, that the orchestrator
      validates it (open PR on the issue branch, matching head, closes-linkage,
      clean worktree, freshness) and owns the single `status:human-review`
      transition, and a pointer to
      `self/.decisions/AgDR-028-orchestrator-owned-terminal-handoff.md`.
      Checkable: `git grep -n "handoff-evidence.json" README.md`.
- [ ] Docs-only diff: no files outside `README.md` change.
- [ ] `uv run --project orchestrator pytest orchestrator/tests -q` passes
      (unchanged suite — evidence the workspace is healthy, attach output).

## Non-goals

- No code, workflow, hook, or spec changes — README only.
- No label changes by the worker (the orchestrator owns the transition; write
  the evidence file as your final action per the workflow prompt).
- Do not merge the pull request — the human merge is part of the checkpoint.

## Consumers of mutated state

- README readers (operators adopting Switchboard).
- The checkpoint record: this issue, its PR, the orchestrator log, and the
  workspace are the pilot's evidence set.

## Assumptions (if false: stop and flag with a comment)

- The workspace is a clean clone of colin-prologue/Switchboard on branch
  `switchboard/issue-<n>` with origin fetched (before_run guarantees).
- `AgDR-028` exists at HEAD (merged 2026-08-08, PR #115).
