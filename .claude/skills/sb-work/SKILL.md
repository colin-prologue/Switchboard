---
name: sb-work
description: Run a Switchboard worker session — a long-running interactive loop that claims tasks from the sb file-queue, dispatches each to a fresh-context subagent in an isolated git worktree at the task's tier, files the validated result, and tears the worktree down. Use to start a worker (one per terminal); the loop self-paces and is killable without data loss.
---

# sb-work — the worker loop

You are a **Switchboard worker session**. You never do task work in your own
context. Each loop pass claims one task, provisions an isolated git worktree,
dispatches a **fresh-context subagent** to do the work at the task's tier, files
the result through the engine, and tears the worktree down. Your context grows
only by loop bookkeeping (~hundreds of tokens/task) — so this session can run
for a very long time and is **disposable**: kill it anytime, start a fresh one,
nothing is lost (all state is on disk).

The `sb` engine is git-free and deterministic. **You own all git operations.**
The engine owns lane state, leases, validation, and result routing. Never parse
a subagent's freeform output — the result *file* is the only channel.

## Setup (once per session)

1. Pick a stable `WORKER_ID` for this session, e.g. `<hostname>-<pid>` or a name
   the operator gave you. Use it for every `sb` call and the ledger filename.
2. Set `W`, the claim wait window in seconds (start at `300`; lengthen on quota
   pressure — see Backoff).
3. Set `MAX_LOOP_ITERATIONS` from the session flag/config (default **200**).
   This is a **diagnostic checkpoint, not a kill** (see Loop checkpoint).
4. Note the **integration base** — the branch this session is on at start
   (e.g. `design/switchboard-v2`). New phase branches are cut from it.
5. `LEDGER=.switchboard/loop-ledger-$WORKER_ID.jsonl`. Initialize `i=0`.

## The loop

Repeat until the operator stops the session:

1. **Claim** (blocks in-tool; costs no tokens while waiting):
   ```
   sb claim --wait $W --worker-id $WORKER_ID
   ```
   - **exit 3** (nothing to claim): the wait window expired with no task. This
     is NOT a loop iteration — do not write a ledger line, do not increment `i`,
     do not run the checkpoint. Just run `sb heartbeat --worker-id $WORKER_ID`
     (liveness for the stale-fleet signal), lengthen `W` if quota is throttled
     (see Backoff), and loop back to step 1. (Idle liveness is the heartbeat's
     job; the loop ledger and checkpoint are about task work only.)
   - **exit 0**: the task JSON is on stdout. Continue with it as `T`.
2. **Heartbeat**: `sb heartbeat --worker-id $WORKER_ID` (feeds the stale-fleet
   signal, spec §7).
3. **Resolve the branch**: `BRANCH = T.context.branch` (authoritative — e.g.
   `sb/plan-001/ph-1`). If absent, fall back to
   `"sb/<plan_id>/<phase_id>".lower()` from `T.source`.
4. **Ensure the branch exists**, then provision the worktree (isolation BEFORE
   any task code runs — PHI-033):
   ```
   git show-ref --verify --quiet refs/heads/$BRANCH \
     || git branch $BRANCH <integration-base>
   WT=.worktrees/$WORKER_ID
   git worktree add "$WT" $BRANCH
   ```
   (If `$WT` already exists from a prior crashed pass, `git worktree remove
   --force "$WT"` first.)
5. **Pick the model**: read `tiers.json` (repo root); `MODEL = tiers["tiers"][T.tier]`.
   Tier lives on the dispatch, not the session — any worker serves any tier.
6. **Dispatch a fresh-context subagent** with the `model` override, working in
   `$WT`:
   - If `T.context.verifies` is set → use **verifier-protocol.md**.
   - Otherwise → use **task-protocol.md**.
   Fill the protocol template from `T` (goal, `done`, constraints, grounding via
   `sb query`, prior result if this is a retry/continuation, the worktree CWD).
   The subagent commits its work to `$BRANCH` and writes
   `.switchboard/results/<T.id>.json` against the result schema. It is the only
   thing that writes that file; you do not.
   - **If the dispatch raises a rate-limit / usage-cap signal** (infra failure,
     not task failure): `sb release <T.id>` (→ queued, attempts unchanged),
     apply Backoff, remove the worktree, record a ledger line with
     `--outcome released --released`, and continue. Do **not** call file-result.
7. **File the result** (the engine validates, moves the lane, enqueues the
   verification task on success):
   ```
   sb file-result <T.id>
   ```
   Capture the returned `lane` as the iteration `OUTCOME`.
   - **If the subagent returned with NO valid result file** at
     `.switchboard/results/<T.id>.json` (a guard hook forced it to stop, or it
     crashed), do **not** leave the task wedged:
     - a **task** subagent (`T.context.verifies` unset) → `sb block <T.id>
       --reason "<why, e.g. guard-forced stop>"`: the engine synthesizes a
       `blocked` result and pauses the task for human (`OUTCOME=paused`).
     - a **verifier** subagent (`T.context.verifies` set) → `sb release <T.id>`:
       a crashed verifier is infra, not human-blockable — re-queue it for
       another verifier (`OUTCOME=released`, set `--released` in the ledger).
8. **Tear down the worktree** (commits persist on the branch; the result lives
   in `.switchboard/`, not the worktree, so teardown is safe even if dirty):
   ```
   git worktree remove --force "$WT"
   ```
9. **Record the iteration** and advance:
   ```
   python -m sb.loopledger append --ledger $LEDGER --i $i \
     --claimed-id <T.id> --type <verify|task> --outcome $OUTCOME --wall-s <elapsed>
   ```
   Use `--type verify` if `T.context.verifies` is set, otherwise `--type task`.
   `i = i + 1`.
10. **Loop checkpoint** (see below). Then repeat from step 1.

## Backoff (quota is advisory, never a claim gate — HDR-011)

`.switchboard/quota.json` (written by sub-plan B's token-free detector; may be
absent in M0) is **advisory only**. If present and it advises a throttled/
exhausted state, **lengthen `W`** (e.g. double it, cap ~1800s) so claims wait
longer between attempts. Never refuse to claim because of quota — a claim is
always allowed; only the wait window changes. On a clean stretch, relax `W` back
toward the 300s default.

## Loop checkpoint (diagnostic, NOT a kill — spec §8)

When `i >= MAX_LOOP_ITERATIONS`:

1. Compute and persist the diagnostic:
   ```
   python -m sb.loopledger diagnose --ledger $LEDGER --worker-id $WORKER_ID \
     --out .switchboard/loop-diagnostic-$WORKER_ID.json
   ```
   It reports total iterations, `distinct_tasks`, `productive` (tasks reaching
   `done`) vs `churn` (`releases + retries`), and total wallclock.
2. Fire a notification so the operator reviews it:
   `sb notify` (the digest/notify layer surfaces it).
3. **PAUSE claiming** — do **not** exit or kill the session. Tell the operator:
   *"Worker $WORKER_ID hit the loop checkpoint at $i iterations — diagnostic
   written to .switchboard/loop-diagnostic-$WORKER_ID.json. Review and resume."*
   Wait for the operator.
4. On **resume**: reset `i = 0` (optionally raise `MAX_LOOP_ITERATIONS` if the
   operator says so) and continue the loop.

Why a checkpoint, not a drift-kill: a raw iteration count conflates healthy high
throughput with unproductive looping. The diagnostic separates the two; at a
high default the common case is "all productive → bump and continue." The sharp
early churn detector (consecutive no-progress) is sub-plan B's tripwire — this
skill owns only the coarse periodic checkpoint.

## Invariants you must not break

- The result file is the only channel from a subagent. Never act on freeform
  subagent output.
- Worktree provisioned **before** dispatch, removed **after** filing — isolation
  is guaranteed, not hoped for.
- A rate-limit on dispatch → `sb release` (attempts unchanged). A verifier
  rejection or attempt exhaustion is the only path to `failed` — and that is the
  engine's job inside `file-result`, never yours.
- The engine does no git; you do no lane-state mutation except through `sb`
  verbs (`claim`, `file-result`, `release`, `block`, `heartbeat`).
