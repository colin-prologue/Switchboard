# AgDR-029: Self-host pilot as a separate supervised binding

- **Status:** accepted (2026-08-08). Implements issue #107's gating decisions.
  Builds on AgDR-028 (orchestrator-owned handoff) and the Stage 1–7
  mixed-provider series (#62–#105).
- **Context:** Switchboard is ready to dogfood mixed-provider execution on its
  own repository. The existing `projects/switchboard-self` binding is
  Claude-only and battle-tested; the mixed machinery is proven only on the
  synthetic canary repository. The first run against the real board must not
  be able to touch anything except its own bounded task.

## Decisions

1. **Separate committed pilot workflow** (`WORKFLOW.pilot-codex.md`) beside —
   never replacing — the production binding. The production path
   (`run-project.sh switchboard-self`) is byte-identical to pre-pilot and IS
   the rollback; no flag flip or config mutation is involved in either
   direction.
2. **Dispatch scope pinned by `required_labels`** (`agent:codex` +
   `gate:triage-passed`): the pilot orchestrator can only ever claim the one
   labeled pilot issue. The live board's other issues are structurally
   invisible to it — supervision by construction, not by operator attention.
3. **Supervised-first, one checkpoint, one bounded docs-only task**, global
   and per-provider concurrency 1, no fallback, weights 100/0. The launcher
   (`run-self-pilot-checkpoint.sh`) preflights CLIs/auth/labels/prereq-issues
   (#47, #57, #61, #109 closed), asserts durable `provider:codex` before
   claim, and requires the AgDR-028 evidence chain (evidence file + orchestrator
   transition log + exactly one PR + clean workspace) before declaring the
   checkpoint passed. The human merge remains the final gate.
4. **The weighted-route checkpoint is deliberately absent** from the launcher:
   #107 gates it on the explicit-Codex checkpoint passing, as a separate
   authorization with its own review.

## Rejected

- **In-place mutation of the production workflow** (add providers to
  `projects/switchboard-self/WORKFLOW.md`): makes rollback a revert instead of
  a launch choice, and puts the mixed config on the path every future
  production run takes before any evidence exists (PHI-047: named artifacts
  govern delegated execution — two named artifacts, two behaviors, zero
  ambient switches).
- **Unattended rollout** (let the scheduled orchestrator pick up mixed config):
  violates PHI-030 (verification before autonomy) — no independent
  verification exists yet for mixed execution on this repository's real
  tasks; the checkpoint IS that verification.
- **Pilot on a fork/mirror instead of the real repo:** the canary series
  already proved the machinery on synthetic assets; the remaining unknowns
  (real board, real labels, real methodology prompt) only exist here.

## Weakest point

`required_labels` scoping depends on nobody else labeling an issue
`agent:codex` + `gate:triage-passed` mid-pilot; the launcher asserts zero open
`agent:codex` issues at start, but a concurrent labeling during the run would
widen scope to that issue. Accepted: the pilot is supervised, short, and
global concurrency 1 means at most one extra claim before the operator stops
it; the #52 transition Action remains the board-side net.

---

## The board-side net changed shape (2026-08-15)

This record accepts a residual partly because #52's **detect-revert** Action
would eventually catch it on the board.

#52's revert was withdrawn. Per-project stances (`AgDR-039`) mean there is no
single legal state machine for its shared transition table to describe, and the
ticket was renarrowed to **detect-only, stance-independent** checks.

The coverage is not gone, but it is weaker, and the difference matters here: a
transient dual-label state is still *detected* (it is one of the three
stance-independent conditions), but nothing corrects it. Anything in the
reasoning below that assumed the board would be put back needs re-reading; the
part that only assumed the divergence would become visible still holds.