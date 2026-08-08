# Mixed-canary preflight

This binding is inert until an operator launches it. It targets only the
separate private `colin-prologue/switchboard-mixed-canary` repository; never
point it at Switchboard or another existing project.

## Current evidence state

The private repository began at `5f48d2c` and completed five reviewed
checkpoints on 2026-07-22. Its `main` is now `14fe89a` with eleven passing
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
5. Stop unless a later checkpoint procedure and its exact routing policy have
   merged. Checkpoints 1 through 5 are complete and must not be rerun.

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

The initial four-checkpoint procedure did not change the mixed workflow's
routing weights. The separately reviewed automatic-Codex checkpoint below also
kept the checked-in `claude: 100, codex: 0` baseline unchanged. Any further live
issue or routing change requires a new reviewed procedure after the Stage 7
observability gate.

## Completed checkpoint 5

Checkpoint `weighted-codex` was the separately reviewed automatic-Codex proof.
It uses `WORKFLOW.weighted-codex.md`, whose `claude: 0, codex: 100` weights
guarantee that one unlabeled issue takes the weighted path to Codex. This split
is not a proposed operating ratio. The normal `WORKFLOW.md` remains at
`claude: 100, codex: 0` and is never edited during the checkpoint.

Issue #9 launched with no `agent:*` or `provider:*` label. Weighted selection
persisted `provider:codex`, dispatched Codex, retained a raw JSONL transcript,
passed eleven tests, and stopped at `status:human-review`. PR #10 merged as
`14fe89a`, closed issue #9 automatically, and its branch was deleted.

Do not rerun checkpoint 5. The dedicated workflow remains inert evidence; the
normal `100/0` workflow remains the only mixed-canary baseline. The Stage 7
observability gate is complete, but no existing project is authorized to launch
mixed mode without a separate reviewed pilot decision.

## Stage 7 circuit checkpoint procedure

The Stage 7 procedure is separate from completed checkpoints 1 through 5. It
must merge before any new issue is created, and each phase must run from the
normal macOS Terminal app with every other Switchboard orchestrator stopped.

Preview either phase without GitHub writes or a process launch:

```bash
scripts/run-stage7-circuit-canary.sh circuit-recovery --dry-run
scripts/run-stage7-circuit-canary.sh rollback-claude --dry-run
```

The first live attempt, issue
[#11](https://github.com/colin-prologue/switchboard-mixed-canary/issues/11),
proved the typed circuit open, provider wait, sole half-open probe, and circuit
close. It then parked without changing the fixture: the canary command override
had replaced Codex's normal headless safety prefix, so the recovered CLI
inherited a read-only sandbox and exhausted three ordinary sessions. Preserve
that issue, its workspace, transcripts, and launcher log as rejected
operational evidence. Do not remove `status:parked` or resume it.

Issue #11 was documented and closed as rejected evidence after the command fix
merged in Switchboard PR #102. The second live attempt, issue
[#12](https://github.com/colin-prologue/switchboard-mixed-canary/issues/12),
proved the corrected workspace-write recovery: Codex changed only the fixture,
passed all 13 tests, committed and pushed `8799707`, opened
[PR #13](https://github.com/colin-prologue/switchboard-mixed-canary/pull/13),
and moved the issue to `status:human-review`. Tracker reconciliation observed
that handoff while the successful half-open worker was still in `after_run`,
cancelled it, and reopened the circuit as an abandoned probe. The launcher
therefore could not reach its circuit-close stop condition and was stopped
manually. Preserve issue #12, PR #13, its workspace, transcripts, and launcher
log as rejected race evidence. Do not merge PR #13 or resume issue #12.

PR #103 corrected successful-worker finalization, after which issue #12 and PR
#13 were documented and closed without merging. The third live attempt, issue
[#14](https://github.com/colin-prologue/switchboard-mixed-canary/issues/14),
again completed the fixture, passed all 13 tests, pushed `5bfaf8c`, opened
[PR #15](https://github.com/colin-prologue/switchboard-mixed-canary/pull/15),
and then cancelled before circuit close. Its transcript established the exact
remaining boundary: the agent changed `status:human-review` before the Codex
CLI emitted `turn.completed`, so reconciliation correctly cancelled a provider
turn that was still active. PR #103 protects the later `after_run` interval and
remains valid, but could not authorize a pre-completion state change. Preserve
issue #14, PR #15, its workspace, transcripts, and launcher log as rejected
contract evidence. Do not merge PR #15 or resume issue #14.

Switchboard PR #104 merged the terminal-handoff correction at `5cd0b9a`.
PR #15 was then closed unmerged, and issue #14 received its failure summary and
closed as rejected evidence without deleting the preserved workspace.

The passing recovery checkpoint used the distinct title
`Stage 7 circuit checkpoint: terminal Codex recovery`, so its duplicate guard
could not mistake any rejected attempt for passing evidence. Its explicit
`agent:codex` label kept the checked-in `100/0` routing baseline unchanged. The
dedicated workflow invoked
`scripts/codex-circuit-canary.sh` with Codex's explicit
`--ask-for-approval never`, `--sandbox workspace-write`, and network
configuration. The wrapper writes one git-excluded outage marker and emits one
structured `service_unavailable` result. The issue remains claimed without
retry or session burn. After the fixed five-minute cooldown, exactly one
half-open probe delegates the complete safety prefix to the real
subscription-authenticated Codex CLI and completes the synthetic fixture task
in the evidence workflow's single allowed scheduler turn. It then writes
`.run/stage7-handoff-ready` instead of changing labels. The one-turn ceiling
enters `after_run` immediately instead of launching a continuation while the
issue remains active. The canary-specific hook requires that marker, a latest
transcript ending in `turn.completed`, a clean pushed branch, a closing PR
body, and exactly one open PR before it owns the `status:human-review`
transition. Cancelled or incomplete turns retain the marker and cannot hand
off.

Live issue
[#16](https://github.com/colin-prologue/switchboard-mixed-canary/issues/16)
satisfied every launcher invariant: typed cooldown open, provider wait, exactly
one half-open probe, circuit close, two session-number-one dispatches, retained
raw transcripts, terminal `turn.completed`, a consumed handoff marker, clean
workspace, and one open handoff PR. Its 13-test fixture
[PR #17](https://github.com/colin-prologue/switchboard-mixed-canary/pull/17)
merged as `b110de1` and closed the issue. Preserve workspace `16` and
`/private/tmp/switchboard-stage7-circuit-recovery.cuulYq/orchestrator-20260727T005405Z.log`
as the accepted recovery evidence.

The rollback phase reused the unchanged `WORKFLOW.rollback-claude.md` and
default CLI mode. Issue
[#18](https://github.com/colin-prologue/switchboard-mixed-canary/issues/18)
retained `provider:codex` as audit history while the log recorded
`provider_id=claude`; all 15 tests passed and
[PR #19](https://github.com/colin-prologue/switchboard-mixed-canary/pull/19)
merged as `e2694d2`, closing the issue. Its final `worker cancelled` follows the
launcher's intentional `shutdown requested` after the human-review stop gate,
not a provider failure. Preserve workspace `18` and
`/private/tmp/switchboard-stage7-rollback-claude.AE4Ehl/orchestrator-20260727T010518Z.log`
as accepted rollback evidence.

Stage 7 is complete. Do not rerun either phase, shorten the circuit cooldown,
induce real subscription exhaustion, or use an existing project without a
separately reviewed pilot plan.

## Accuracy note — Stage 7 circuit evidence (issue #109, 2026-08-08)

The pre-#109 canary injected `{"type":"error","error":{"code":"service_unavailable",…}}`
— a hand-authored shape carrying an `error.code`. Ground-truth capture
(`orchestrator/tests/fixtures/codex_cli_auth_401.jsonl`, codex-cli
0.146.0-alpha.3.1) shows real Codex failure events carry **no code**: the
Stage 7 live evidence (AgDR-026, PRs #99–#104) therefore exercised the
`_CODEX_CODES` lookup path, which real Codex traffic never takes. The circuit
*mechanism* proven by that evidence (open/pause/recover concurrency) is not in
doubt; its classification input shape was unrealistic. The canary now injects
a real-shaped `turn.failed` (message-only, no code), exercising the
`_TEXT_PATTERNS` path production traffic actually uses. Its message classifies
`PROVIDER_UNAVAILABLE` (a cooldown class) rather than the captured auth text:
authentication latches the circuit and would never reach the cooldown/half-open
recovery this canary proves (codex review, PR #113). No real
service-unavailable string is captured yet (fixtures/README.md lists it
unverified), so the injected text is pattern-matching by construction — shape
real, unavailable-text still synthetic. Re-validation of the
Stage 7 checkpoints was deliberately NOT re-run (per #109 non-goals).
