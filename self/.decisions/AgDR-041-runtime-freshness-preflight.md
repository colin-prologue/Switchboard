# AgDR-041: Runtime freshness — recompose config from origin, surface stale code

- **Status:** accepted (2026-08-13). Issue #32. (Drafted as 038; renumbered to
  041 on merge, when 038–040 landed on `main` from the stance-ladder work while
  this branch was open. The gap-avoidance heuristic that picked 038 over the
  free 036 did not help — the collision came from work that started later and
  merged sooner, which numbering cannot anticipate.)

## Context

The orchestrator is a long-running process, one per registered project, all
sharing a single checkout. Two different kinds of staleness accumulate in it,
and they have opposite remedies:

- **Config** (the composed workflow) is hot-reloadable. `_maybe_reload` stats
  the loaded file and re-parses it when the mtime moves, so a config change can
  be adopted with no restart at all.
- **Code** (`orchestrator/src/**`) is already imported into the running process.
  No amount of fetching adopts it; only an operator-controlled restart does.

Before this change the process had no route to either. `hooks/before_run.sh`
fetches the base branch per attempt, and #57 gave a *reused workspace* its
`.sb-staleness` + ff-only contract — but that is the worker-facing workspace,
not the orchestrator's own checkout. The checkout side had nothing.

## Decision

A single standalone script, `scripts/freshness-preflight.sh <slug>`, does both
jobs without ever mutating the checkout — no pull, no merge, no checkout, no
restart, no tracked-file write, HEAD never moves.

1. **Recompose config from origin.** The project's template is read straight out
   of `origin/<ref>` via `git show` and rendered into the gitignored, per-project
   `$SB_HOME/.run/<slug>/composed-WORKFLOW.md`. `run-project.sh` execs against
   that path **unconditionally**.
2. **Signal stale code.** One pathspec-limited
   `rev-list --count <loaded-sha>..origin/<ref> -- orchestrator/src/` subsumes
   both *checkout behind origin* and *process behind checkout*. Non-zero writes
   a per-project `restart-needed.json`; zero deletes it. Nothing in dispatch,
   eligibility, or reconcile reads it — it is operator-facing only, and the
   launch path prints it because launch is the one moment it is cheaply
   actionable.

Four choices inside that shape are load-bearing:

- **Staleness is measured from the LOADED sha, not HEAD.** `run-project.sh`
  captures `SB_LAUNCH_SHA` immediately after exporting `SB_HOME`. A HEAD-based
  detector self-clears on the operator's own `git pull` while the process is
  still running pre-pull code — precisely the blind spot the signal exists to
  close. There is a test that pins this exact scenario.
- **The fallback is content-level and initialization-only.** On any fail-open
  path the preflight seeds the composed file from the tracked `WORKFLOW.md`
  *only when it does not yet exist*. Seeding an existing file would bump its
  mtime, parse cleanly, and silently revert a running process from origin's
  config to the committed snapshot — a silent un-adoption, the inverse of this
  ticket's own bug class. Path-level fallback is likewise rejected: the
  scheduler resolves the watched path once at startup, so switching paths would
  leave later recomposes unwatched.
- **The rename is conditional on differing bytes.** Comparison is against this
  routine's own freshly composed output, never the tracked blob — so every
  origin change is adopted on the first pass and a hand-edited composed file is
  overwritten. An unconditional rename would defeat the mtime guard and log
  ~864 spurious `workflow reloaded` lines/day across three processes, inverting
  the signal this ticket exists to keep loud.
- **Both `.run/` artifacts are per-project.** One checkout runs three processes.
  An unscoped composed path lets the last writer's repo binding hijack every
  process; a checkout-scoped marker reports clean for all three the moment any
  one of them restarts.

Every fail-open path exits 0. The script runs under `run-project.sh`'s
`set -euo pipefail`, so a non-zero fail-open would be a hard launch refusal —
i.e. not fail-open at all. Non-zero is reserved for usage errors.

## Rejected alternatives (steelmanned)

- **Auto-pull / auto-restart on detecting staleness.** Genuinely closes the loop
  with no operator in it, and is what "freshness" naively suggests. Rejected:
  restarting a process mid-session kills in-flight agent turns, and a checkout
  shared by three processes cannot be pulled on one process's behalf without
  changing code under the other two. Surfacing is the whole remedy; acting is a
  separate, operator-owned decision.
- **A Python module inside `orchestrator/src/`, called in-process.** Better
  typing and no subprocess. Rejected on a bootstrapping ground: the module would
  itself be code the running process already loaded, so it could not be tested
  from the bare skeleton the launch tests use, and a `python -m` invocation is
  unavailable there. Shell also matches the surrounding launch-path idiom.
- **Compose into the tracked `projects/<slug>/WORKFLOW.md`.** Removes the
  two-file split. Rejected as an explicit non-goal: it makes the routine a
  tracked-file writer, would dirty the checkout on every tick, and would fight
  the CI compose-drift check. Generated scratch belongs in gitignored `.run/`.
- **A hand-edit skip rule on the composed file.** Superficially protects an
  operator debugging by hand. Rejected: a silent refusal to adopt origin is
  exactly the bug class this ticket exists to kill, and the file is documented
  generated scratch.

## Blast radius

`scripts/freshness-preflight.sh` (new), `scripts/run-project.sh` (launch sha
capture, preflight call, composed exec path, marker print), `SETUP.md`, and two
test modules. No Python changes; no orchestrator behaviour change beyond *which
file path* is passed to `--workflow`. The tracked `WORKFLOW.md` remains both a
hard launch precondition and the fail-open seed source.

**No binding format change, and that is load-bearing.** The routine reads only
keys `register-project.sh` already wrote, and derives `max_concurrent_agents`
from the tracked `WORKFLOW.md` the way `verify-setup.sh` does. So it adopts
origin for projects registered *before* it existed, with no re-registration —
a freshness mechanism that first requires you to re-run the thing you were
trying to stop re-running would not be one. The draft of this change did add
`SB_MAX_AGENTS` and `SB_WORKFLOW_TEMPLATE` to the emitted binding plus three
backfills; merging against the stance ladder removed all four.

This ticket is the checkout-side sibling of #57's workspace-side contract, and
it deliberately does **not** claim to solve workspace staleness. The per-tick
caller (scheduler wiring, `freshness.*` config keys, cadence gate, timeout) is
split out to **#131**, chained behind this issue; this PR ships the script and
its launch caller only.

## Weakest point (accepted)

The CI drift check validates the **tracked** `projects/<slug>/WORKFLOW.md`,
which this routine never writes. So the thing CI validates and the thing the
process runs can diverge indefinitely with no signal: CI keeps checking a
snapshot while the orchestrator runs something recomposed from origin. That is
inherent to composing into `.run/` and is not fixed here.

What the merge against the stance ladder *did* fix is the sharper version of
it. The draft duplicated `max_concurrent_agents` into `project.env` as
`SB_MAX_AGENTS` while `verify-setup.sh` back-derived it from the tracked
workflow — two sources for one value, seeded equal and free to drift apart
silently thereafter. Reading it from the same place `verify-setup.sh` does
removes the second source, so the two routines now compose from identical
inputs by construction rather than by a backfill assertion.

One asymmetry remains and is accepted: the template is read from **origin**
while the values bound into it are read from the **local** `project.env`,
mirroring `verify-setup.sh`. An origin-side *binding* change is therefore not
adopted without a pull. That is the seam to revisit if the composed output ever
becomes the thing CI validates.
