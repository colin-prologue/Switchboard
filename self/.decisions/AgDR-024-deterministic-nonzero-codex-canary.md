# AgDR-024: Deterministic nonzero-Codex canary

**Status:** proposed (2026-07-21)
**Surfaces:** isolated mixed-canary workflow, native checkpoint launcher,
automatic weighted assignment, and rollout evidence

## Context

Stage 6 Slice 4 proved explicit Claude and Codex assignments, automatic
`claude: 100, codex: 0` selection, and rollback to the unchanged Claude-only
process. AgDR-023 requires a separately reviewed nonzero Codex weight before
mixed routing can progress.

A low fixed Codex percentage does not guarantee that one synthetic issue's
stable SHA-256 bucket selects Codex. Creating issues until one happens to land
in that bucket would add unrelated tracker mutations and make the checkpoint's
duration nondeterministic. Changing the baseline workflow in place would also
make rollback depend on restoring a file before another launch.

## Decision

Add one dedicated, inert `WORKFLOW.weighted-codex.md` to the existing private
mixed-canary binding. It keeps both providers configured but uses
`claude: 0, codex: 100`. Checkpoint 5 creates one unlabeled synthetic issue and
launches that workflow with explicit `--provider mixed`.

The `0/100` split is an evidence mechanism, not a proposed operating ratio. It
guarantees that the normal weighted-selection path writes `provider:codex` and
dispatches Codex without an `agent:codex` override. The checked-in
`WORKFLOW.md` remains `claude: 100, codex: 0`, so stopping checkpoint 5 restores
the safe baseline without a file edit or deployment change.

Checkpoint 5 remains one-at-a-time and native-terminal only. It requires all
four earlier checkpoint issues to be closed, refuses any existing open issue or
pull request, stops at the existing named terminal states, requires a clean
workspace and one handoff PR, and verifies at least one Codex JSONL transcript.

## Verification gate

Before live launch:

1. Review and merge the workflow, checkpoint contract, launcher change, and
   offline regression coverage together.
2. Confirm the primary checkout is clean and on `main`.
3. Run only `weighted-codex --dry-run` from the normal macOS Terminal and
   inspect its declared repository, workflow, labels, prerequisites, and
   expected provider.
4. Return for review before running the same phase live.

A live pass requires `provider_id=codex`, durable `provider:codex`, no
`agent:*` label, passing fixture tests, a raw Codex transcript, and a scoped PR
at `status:human-review`. Merge and issue closure remain human gates.

## Rejected options

- **Use a low percentage and accept whichever provider the first issue gets.**
  This loads a nonzero policy but may not exercise automatic Codex dispatch.
- **Create issues until a low-percentage bucket selects Codex.** This is
  nondeterministic and produces irrelevant issue history.
- **Temporarily edit the baseline workflow in place.** This adds a restoration
  step to rollback and increases the chance that a later operator launches the
  wrong weights.
- **Reuse `agent:codex`.** Explicit Codex assignment already passed and does not
  prove weighted selection.

## Blast radius

Only the private `colin-prologue/switchboard-mixed-canary` repository is in
scope. No existing project binding, production process, default CLI mode, or
baseline mixed workflow changes. The new workflow is inert unless an operator
selects checkpoint 5 from the native launcher.

## Weakest point

The deterministic `0/100` proof does not measure behavior at a low traffic
share or with both providers running concurrently. Those are later rollout and
Stage 7 operational-hardening gates; passing this checkpoint does not authorize
mixed mode on an existing project.
