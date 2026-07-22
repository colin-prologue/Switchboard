# Mixed-canary preflight

This binding is inert until an operator launches it. It targets only the
separate private `colin-prologue/switchboard-mixed-canary` repository; never
point it at Switchboard or another existing project.

## Current evidence state

The private repository began at `5f48d2c` and completed the four reviewed
checkpoints on 2026-07-21. Its `main` is now `18c8e19` with nine passing
standard-library tests. The `switchboard-agent` installation has contents,
issues, and pull-request write access, and all gate-state, `agent:*`, and
`provider:*` labels remain provisioned.

Before creating a dispatchable issue or starting `--provider mixed`:

1. Create the private repository and grant the `switchboard-agent` GitHub App
   installation access.
2. Preview the complete label set without touching GitHub:

   ```bash
   scripts/provision-mixed-canary-labels.sh --dry-run
   ```

3. Provision the labels with an authenticated operator `gh` session:

   ```bash
   scripts/provision-mixed-canary-labels.sh
   ```

   The command is idempotent. In particular, `provider:claude` and
   `provider:codex` must exist before launch: mixed dispatch writes one of them
   before claiming an issue. The `agent:claude` and `agent:codex` labels enable
   the later explicit-assignment evidence tickets.
4. Confirm the reviewed synthetic fixture tests pass on `main`.
5. Stop unless the next checkpoint procedure and its exact routing weights have
   merged. Checkpoints 1 through 4 are complete and must not be rerun.

The initial workflow remains `claude: 100, codex: 0`. Provisioning labels does
not change that routing policy, create an issue, or start a worker.

## Completed native checkpoint procedure

Run checkpoints only from the normal macOS Terminal app. The Codex Desktop
child sandbox cannot write workspace Git metadata, so it is not valid live
evidence. Keep every other Switchboard orchestrator stopped.

Each invocation creates exactly one issue, launches one foreground-managed
orchestrator, and stops it when that issue reaches a named outcome. It refuses
to start if the repository is public, required labels are absent, the primary
Switchboard checkout is dirty or off `main`, another issue or PR is open, the
same checkpoint already exists, or an earlier checkpoint is not closed.

| Checkpoint | Issue routing | Evidence |
| --- | --- | --- |
| `explicit-claude` | `agent:claude` through mixed mode | Durable `provider:claude` and Claude dispatch |
| `explicit-codex` | `agent:codex` through mixed mode | Durable `provider:codex`, Codex dispatch, raw JSONL |
| `weighted-claude` | No override, weights `100/0` | Durable `provider:claude` from weighted selection |
| `rollback-claude` | Existing `provider:codex`, default CLI mode | Claude dispatch without rewriting the audit label |

All four checkpoints passed from the normal macOS Terminal. Issues #1, #3, #5,
and #7 reached `status:human-review`; their PRs #2, #4, #6, and #8 merged and
closed the issues. Explicit assignments dispatched Claude and Codex, the
unlabeled `100/0` policy routed to Claude, and the default rollback dispatched
Claude while preserving `provider:codex` as audit history.

The launcher stopped at each handoff and all worker branches were deleted after
merge. During rollback, one poll briefly observed both `status:todo` and
`status:in-progress` between label writes; the next poll settled and no
duplicate worker launched.

This procedure does not change the mixed workflow's routing weights. Moving
Codex above zero remains the next separately reviewed rollout. Before another
live issue is created, review a procedure that chooses the exact weights and a
deterministic evidence strategy, preserves the one-checkpoint stop conditions,
and restores the checked-in `claude: 100, codex: 0` baseline.

## Proposed checkpoint 5

Checkpoint `weighted-codex` is the separately reviewed automatic-Codex proof.
It uses `WORKFLOW.weighted-codex.md`, whose `claude: 0, codex: 100` weights
guarantee that one unlabeled issue takes the weighted path to Codex. This split
is not a proposed operating ratio. The normal `WORKFLOW.md` remains at
`claude: 100, codex: 0` and is never edited during the checkpoint.

Do not launch checkpoint 5 until its procedure PR merges. Then refresh the
primary checkout and preview only this phase from the normal macOS Terminal:

```bash
git -C "$HOME/Developer/Switchboard" pull --ff-only origin main
bash "$HOME/Developer/Switchboard/scripts/run-mixed-canary-checkpoint.sh" \
  weighted-codex --dry-run
```

The preview must declare mixed mode, no `agent:*` issue label, the dedicated
weighted-Codex workflow, expected dispatch and durable provider `codex`, and all
four completed checkpoints as prerequisites. It performs no GitHub writes and
launches no process. Return to the migration session with that output before
running the phase live.
