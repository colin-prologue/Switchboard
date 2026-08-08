# AgDR-028: Orchestrator-owned terminal handoff via validated evidence contract

- **Status:** accepted (2026-08-08). Implements issue #61; supersedes the
  worker-performed handoff relabel (WORKFLOW step 7, pre-#61) and the Stage 7
  canary's hook-owned transition (`stage7-circuit-after-run.sh`, AgDR-026 era).
- **Context:** Issue #59 ended with two `status:*` labels because the worker
  prompt said "move the label" and the agent added without removing. Stage 7
  then showed the deeper race: an agent can expose `status:human-review` while
  its provider turn is still active or later fails — the label claim and the
  provider outcome are separate events with no ordering guarantee. The
  scheduler previously disclaimed ownership of handoff labels (workers wrote
  them); the canary patched the race with a canary-only after_run hook — a
  parallel implementation of a production-shaped problem.

## Decision

1. **Workers never mutate `status:*` labels.** A worker's final task action is
   writing `.run/handoff-evidence.json` (`{"issue", "pr_number", "head_sha"}`)
   — a git-excluded, workspace-local contract (`orchestrator/handoff.py`).
2. **The orchestrator owns the terminal transition.** In the worker loop, ONLY
   the provider-success branch validates evidence, and only after
   `TurnResult.status == "succeeded"`: parse → issue linkage → workspace HEAD
   equals evidence sha → exactly one open PR for the branch, head oid equal,
   `closingIssuesReferences` covering the issue. Valid evidence triggers
   `tracker.set_sole_status_label(issue, status:human-review)` — one tracker
   mutation from the orchestrator's perspective, with mandatory read-back
   verification (issue #44 claim/state-divergence class). Every failure /
   cancellation / timeout path bypasses validation entirely, and evidence
   written mid-turn is inert until the result arrives — that ordering closes
   the Stage 7 race by construction.
3. **Rejections are diagnostics, never transitions.** Malformed/stale
   evidence, missing/multiple/mismatched PRs, or missing close-linkage log a
   structured rejection; the issue keeps its status, the workspace is
   preserved, and the session continues so the agent can repair within its
   existing budget.
4. **The canary hook shrinks to an artifact-capture passthrough.** The
   mixed-canary agent writes the same production contract; checkpoint
   assertions now prove the production path (evidence file present +
   orchestrator transition log line) instead of a parallel hook
   implementation.

## Rejected

- **Prompt-only fix** (tell agents to swap labels correctly): agents remain
  the writer; the #59 dual-label failure and the Stage 7 active-turn race both
  survive prompt wording. Provenance stays self-reported.
- **after_run-hook ownership** (generalize the canary hook): hooks run on
  success, failure, AND cancellation, so hook execution is not proof of
  terminal success; validation would live outside the component that knows
  the provider result. Also keeps handoff logic per-project bash rather than
  tested provider-neutral Python.
- **PR-merge-driven closure only** (skip human-review label): abandons the
  Gate C review queue the whole board model is built on.

## Weakest point

`set_sole_status_label` is add-then-remove under GitHub's non-atomic label
API: a crash between the two calls can leave a transient dual-label state.
The read-back verification catches divergence in-session, the #14 claim
machinery tolerates dual labels (sorted-first normalization), and #52's
detect-revert Action is the eventual board-side net — accepted as bounded.

Evidence: `orchestrator/tests/test_handoff.py` (validation matrix),
`test_integration.py` issue-#61 block (success swap / Stage 7 race /
rejection-no-transition), `test_stage7_circuit_canary.py` passthrough test.
