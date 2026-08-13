# AgDR-036 — One orchestrator per checkout, enforced by a process-held flock

- **Status:** proposed (ratify at the merge gate)
- **Issue:** #130
- **Date:** 2026-08-13

## Context

On 2026-08-09 two orchestrator processes ran concurrently against
`switchboard-self`: the post-#128 replacement launched ~04:56Z, and pid
80021/80032 — the pre-#128 production process started 21:57 EDT, which
**survived** the restart because no launcher holds anything. At `d097571`,
`scripts/run-project.sh` contains no `pgrep`, no `flock`, no pidfile; the only
matches anywhere in `scripts/` are the two advisory `pgrep` preflights in
`run-self-pilot-checkpoint.sh` (`:52`, `:104`), which are operator hints in a
different script and guard nothing at the process level.

Every consequence followed from the duplication, not from any single-process
bug: one verify dispatch **per process per issue** (05:14:51Z) producing paired
verdicts ~13 s apart on #32 and #126, per-role budgets burning at 2x, park
comments in two formats within one minute (B's in-memory pre-#128 modules still
emitted the `session cap reached` literal that PR #128 removed from the tree),
and a sticky re-park loop on every operator unpark because B's counters never
reset. The originally filed hypothesis — #47 incomplete-turn continuation
double-posting — is ruled out: continuation is `--resume <session_id>` of the
same provider session (`runner.py`, `Continuation.RESUME_SESSION`); it restores
context and cannot re-run a review from scratch.

An advisory preflight in a shell script cannot fix this. The survivor is a
*process*, so the exclusion has to be held by a process.

## Decision

The orchestrator process itself takes an `fcntl.flock(LOCK_EX | LOCK_NB)` on
`<workflow parent>/.run/orchestrator.lock` as the **first statement** of
`Orchestrator.run()`, and holds it for its lifetime
(`orchestrator/src/orchestrator/singleton.py`). On conflict `run()` raises
`SingletonLockError` naming the lock path and the holder's pid.

Four properties are load-bearing, each chosen against a plausible alternative:

1. **Held by the process, not the launcher.** An orphaned survivor still holds
   it, so a new launch fails against the *real* holder. All four launchers
   (`run-project.sh`, `run-self-pilot-checkpoint.sh`,
   `run-mixed-canary-checkpoint.sh`, `run-stage7-circuit-canary.sh`) inherit
   the guard for free and none of them change.

2. **Keyed on the workflow's PARENT DIRECTORY.** `--workflow` is the process's
   only project identity — `main.py` parses nothing else and the process never
   reads `SB_HOME`. `projects/switchboard-self/WORKFLOW.md` and
   `WORKFLOW.pilot-codex.md` therefore share one lock, making the pilot and
   production self-orchestrators mutually exclusive. Filename keying would give
   them separate locks and reproduce the incident exactly.

3. **The fd is retained, never left to refcounting.** `flock` rides the open
   file *description*: a builtin `open()` object falling out of scope is closed
   by CPython refcounting and releases the lock before `_load_workflow` even
   runs — a silent no-op guard. The helper returns the raw `os.open` fd (never
   auto-closed) and the `Orchestrator` stores it. This is the difference
   between the two otherwise-identical implementations, so it has its own test
   (`test_acquired_lock_is_held_against_a_probing_subprocess`), verified to
   fail against a refcounting variant.

4. **Pid published in the file, not queried from the lock.** BSD `flock` has no
   query interface and `fcntl.lockf` is a different, non-interacting lock
   family that must not be mixed in. The acquirer `ftruncate`s and writes
   `os.getpid()`; the refuser reads it and falls back to the literal `unknown`
   when the file is empty or unparseable (an empty file is the normal race
   window, not an error). The pid named is the **python child**, not the `uv`
   wrapper the operator's process tree shows — the incident recorded
   80021/80032 for exactly this reason — and the message says so.

Staleness needs no machinery: `flock` releases on process exit including
`kill -9`, so a hard-killed orchestrator never wedges the next launch.

## Rejected options (steelmanned)

- **A pidfile with liveness checking (`kill -0`).** Portable and greppable, and
  it can answer "who holds this?" without a side-channel. Rejected: it is
  racy by construction (write, crash, stale file; or pid reuse), and every
  correct version reinvents the staleness heuristics `flock` gets for free from
  the kernel. The incident's failure mode was precisely a survivor the operator
  believed was gone — a heuristic is the wrong tool for that.
- **`pgrep` preflight in every launcher (extending
  `run-self-pilot-checkpoint.sh`'s pattern).** Cheapest change, zero new code
  paths. Rejected: the guard would live in the shell, so anything that starts
  the module directly bypasses it, and it must be duplicated into four scripts
  that can drift. The checkpoint scripts keep theirs as operator hints; the
  lock is the enforcement.
- **The `flock(1)` CLI wrapping the launcher command.** One line per script.
  Rejected twice over: absent on stock macOS, and it binds the lock to the
  *shell*, which is the survivor problem again.
- **Keying the lock on `SB_GITHUB_REPO`.** This is what the hazard is actually
  scoped to (see weakest point). Rejected: the process does not read that env
  var, and adding an identity input to make the lock work would put config
  parsing in front of the guard — the opposite of "first statement".

## Blast radius

- `Orchestrator.run()` gains a failure mode before `_load_workflow`. `run()` is
  reached by no test in the suite (`test_main.py` substitutes a stub
  orchestrator), and every orchestrator a test constructs is bound to a
  `tmp_path` workflow, so even a future test that calls `run()` locks under its
  own `tmp_path/.run/`.
- The refusal **raises** rather than `sys.exit()`ing: `SystemExit` inside the
  coroutine is a `BaseException` that asyncio re-raises past `main.py`'s
  log-and-return-1 handler, handing the operator a traceback instead of the
  refusal. A raised exception travels as `str(exc)` through the existing
  handler and exits non-zero.
- The canary scripts gain a new **hard-failure** mode:
  `run-mixed-canary-checkpoint.sh` and `run-stage7-circuit-canary.sh` background
  orchestrators behind `kill -TERM` cleanup traps, so a checkpoint whose
  predecessor did not die cleanly now fails at launch instead of silently
  racing. That is correct; naming it here so the first such failure reads as
  the guard working.
- One new artifact per project checkout: `<workflow parent>/.run/`, covered by
  `.gitignore`'s `.run/` at any depth.

## Weakest point

The lock is **checkout-scoped** while the duplicate-dispatch hazard is
**repo-scoped**. Two different checkouts running the same project against the
same GitHub repo each take their own lock and race exactly as the incident did.
Accepted under the single-operator, single-checkout premise — the incident
itself was one checkout — and because the only repo-scoped identity available
(`SB_GITHUB_REPO`) is env the process does not read. If multi-checkout
operation arrives, this decision re-opens.
