# Mixed-canary preflight

This binding is inert until an operator launches it. It targets only the
separate private `colin-prologue/switchboard-mixed-canary` repository; never
point it at Switchboard or another existing project.

## Provisioned baseline

The private repository is seeded on `main` at `5f48d2c` with `greeting.py`, one
passing standard-library test, no dependencies, and `.run/` ignored. The
`switchboard-agent` installation has contents, issues, and pull-request write
access. All gate-state, `agent:*`, and `provider:*` labels are provisioned. No
canary issue or worker has launched yet.

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
4. Seed the reviewed synthetic fixture and confirm its tests pass on `main`.
5. Stop. Do not launch the mixed process until the checkpoint procedure below
   is merged.

The initial workflow remains `claude: 100, codex: 0`. Provisioning labels does
not change that routing policy, create an issue, or start a worker.

## Native checkpoint procedure

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

After the procedure PR merges, refresh the primary checkout and preview only
the first checkpoint:

```bash
git -C "$HOME/Developer/Switchboard" pull --ff-only origin main
bash "$HOME/Developer/Switchboard/scripts/run-mixed-canary-checkpoint.sh" \
  explicit-claude --dry-run
```

The preview performs no GitHub writes and launches no process. When its output
looks correct, run the same command without `--dry-run`. The command stops at
`status:human-review`, `status:parked`, `status:blocked`, `status:drafting`,
`status:plan-review`, a 30-minute timeout, or unexpected orchestrator exit. A
non-review stop is a failed checkpoint, not permission to continue.

On a pass, review and merge the synthetic handoff PR and confirm the issue is
closed. Return to the Switchboard migration session before running the next
checkpoint. Do not run multiple phases in one terminal session or skip ahead.
If interrupted, do not rerun the phase: preserve the workspace and issue, then
return with the terminal output for recovery.

This procedure does not change the mixed workflow's routing weights. Moving
Codex above zero remains a later, separately reviewed rollout after these
checkpoints produce clean evidence.
