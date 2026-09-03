# AgDR-2026-09-03-fleet-health-is-an-external-observer-with-a-durable-window

- **Status:** proposed (ratify at the adopting PR's merge gate)
- **Issue:** #193
- **Date:** 2026-09-03
- **Supersedes / amends:** nothing. It gives SETUP.md Stage 5b's four grep
  recipes a mechanical owner; the recipes stay as the explanation of what the
  check does.

## Context

The fleet is launchd-supervised, and supervision restarts crashes without
detecting degradation — SETUP said so outright ("**Wedged ticks — `KeepAlive`
cannot detect this one.**"). Detection was four grep recipes and the operator
remembering to run them. Under `AgDR-048` there is no second human to remember,
so folklore that depends on looking is not detection.

The ticket fixed the shape (external observer, four detections, local report,
no auto-remediation) and left the mechanism open. Three choices inside it are
not derivable from the ticket, and this record is about those.

## Decision

**1. A stdlib-only Python module in the orchestrator package, invoked by a shell
wrapper that runs a bare `python3`.**

The check exists to be believable when the fleet is dead, and "the fleet is
dead" includes "the project virtualenv is what broke". `scripts/fleet-health.sh`
therefore sets `PYTHONPATH` and execs `python3 -m orchestrator.fleet_health`
rather than `uv run` — the same argument `disabling_defaults` already makes for
its own bare-`python3` invocation. The module imports nothing outside the
standard library except `.log`, which is stdlib-only itself, and a test asserts
that (no `httpx`, no `urllib`, no `socket`, no `tracker`, no `scheduler`). The
network invariant is checked structurally because a mock cannot prove absence.

**2. The observation state carries the last *measured* window, and a run inside
`min_window_s` neither advances the baseline nor recomputes.**

Crash-loop and wedged are rates, so they need a previous observation. The naive
version — advance the baseline every run — makes the check self-falsifying: the
operator runs it, sees WEDGED, runs it again five seconds later to confirm, and
the second run compares against a five-second window and reports a fleet that
recovered while they were reading. So `.run/fleet-health-state.json` stores
`{counts, observed_at, window}`, and a run whose elapsed time is under the
minimum reuses the stored `window` verbatim and writes the previous row back
unchanged. Back-to-back runs are then identical *by construction*, not by
coincidence of the deltas being small.

Two other paths degrade into the same "no rate available": no previous row at
all, and counts that went backwards (the log was truncated or rotated — #12 owns
rotation; this check only has to survive it, and a negative delta must never
render as a clean fleet).

**3. The headline state per slug is chosen by a declared precedence, and
`notice` is a first-class level that never sets the exit status.**

A slug can hold four findings at once and the report needs one word.
`PRECEDENCE = (DOWN, CRASH-LOOP, WEDGED, STALE-CODE, UNKNOWN, OK)` is committed
in the module with its argument attached, applying the rule
`AgDR-2026-08-29-state-precedence-is-declared-not-alphabetical` set for
`status:*` labels to a second state vocabulary. `UNKNOWN` above `OK` is the load-
bearing one: "we could not see" must never render as "fine".

The `degraded` / `notice` split is what keeps the check from crying wolf. A
marker written twenty minutes ago, three `ReadTimeout`s in ten minutes, and a
registered project nobody supervises are all real observations and none is worth
an exit-1. Only `degraded` findings set the status, which is the signal a
wrapping interval job escalates on.

**A consequence worth stating: the startup identity supersedes marker age in
both directions.** When the running process's `orchestrator starting` record
carries a known `sha=` and the marker carries a `target_sha`, the comparison
*replaces* the age test — a nine-hour-old marker whose target matches the
running sha is reported as superseded, at notice level. Marker age is the
fallback for `sha=unknown`, not the primary. This is only safe because #131
re-runs the freshness preflight on a tick cadence (first statement of `_tick`,
so even a tick that throws afterwards keeps refreshing), which means a running
process's marker is live rather than a launch-time fossil.

## Rejected options, steelmanned

**A pure shell script.** The strongest case of the three: it is what the four
recipes already are, it adds no Python surface, and it would be readable by
anyone who can read the SETUP section it replaces. Rejected on two counts.
The arithmetic — deltas against durable state, expected-ticks-per-window, a
JSON report — is where shell stops being the simple option and starts being
`awk` nobody will edit. And the worker allowlist admits exactly two pytest
invocations, so a shell implementation's detections would be verifiable only
through a subprocess harness that is itself Python; the tests would have landed
in the same place with the logic one indirection further away.

**`uv run --project orchestrator` in the wrapper.** Consistent with every other
entry point in the repo, and it would let the module grow a dependency later.
Rejected because it makes the observer depend on the resolver and the
environment of the thing it observes. The failure mode is specific and bad: the
one morning `uv` cannot resolve is a morning the fleet is probably also unwell,
and the check reports nothing at all rather than reporting the fleet.

**Advance the baseline every run, and suppress rate findings when the window is
too short.** Simpler state (no stored window) and it does stop the check
manufacturing findings. Rejected because it does not satisfy what the ticket
actually asked for — the second of two back-to-back runs would report `OK` where
the first reported `WEDGED`, which is worse than a false positive: it is a false
*clear*, and it appears exactly when the operator is double-checking a real one.

**Compare the running sha against local `origin/main` directly, instead of
against the marker's `target_sha`.** The ticket permits it and it is strictly
more information — it would detect staleness with no marker present at all.
Rejected for this PR because it makes the check read the git checkout (a ref, a
pathspec-limited `rev-list`) and re-derive a policy `freshness-preflight.sh`
already owns, which is where the two would drift. The marker is that script's
published answer; consuming it keeps one definition of "behind".

## Blast radius

- **New surfaces only:** `orchestrator/src/orchestrator/fleet_health.py`,
  `scripts/fleet-health.sh`, `orchestrator/tests/test_fleet_health.py`. Nothing
  imports the module; nothing runs the script unless the operator installs the
  interval job.
- **Two files written, both under the gitignored `.run/`:**
  `fleet-health.json` (the report — the operator-inbox digest ticket is its
  designed second reader) and `fleet-health-state.json` (this module's own
  baseline). Neither is read by the orchestrator. No orchestrator-owned file is
  written, and a test snapshots the whole tree to pin that.
- **Three emission sites gained comments, no behaviour:**
  `scripts/run-project.sh` (the banner), `scheduler.py` (`orchestrator
  starting`, `tick error`). Each says that its record *shape* is now a detection
  surface and names what to update alongside a rename.
- **SETUP.md Stage 5b rewritten** around the check, with the two defects triage
  found fixed: the drifted `scheduler.py:450-451` citation (dropped rather than
  re-pinned — a line number in prose is what drifted in the first place) and the
  over-matching `grep -c 'tick error'` recipe, now anchored to the timestamp.
- **Not touched:** the orchestrator process, dispatch, the freshness preflight,
  the plist template, log rotation (#12), per-issue visibility (#174).

## Weakest point

**Every threshold is a judgment call with no production baseline, and the
wedge rate is the one most likely to be wrong.** `≥ 3` new banners, `≥ half the
expected ticks`, `4h` marker age, a `60s` minimum window: each is defensible in
prose and none has been measured. The wedge ratio is the exposed one, because it
has to separate two things that look identical in a ten-minute window — a
genuinely wedged process failing every tick, and a bad ten minutes of transport
flakes. On this repo's own log the flake rate is several per day; at a 10-minute
cadence with a 30s poll interval, ten expected ticks means five errors trips
WEDGED, and a burst of five `ReadTimeout`s in one window is not obviously
impossible. Every threshold is a CLI flag for that reason.

**What would make this wrong:** the first WEDGED finding an operator
investigates turns out to be a transport burst. The fix is the ratio or the
cadence, not the detection — but if it happens twice, the rate model is wrong
and the check should be classifying the `error=` field (a `ReadTimeout` is not a
`RuntimeError`) rather than counting records. That is deliberately not built
here: classifying failure kinds from the log is a second detector, and building
it before a single false alarm has been observed would be tuning against an
imagined distribution.

**Second weakest point: the check runs under the same supervisor it watches, and
cannot certify itself.** A dead interval job is silent in exactly the same way a
dead orchestrator is, and this module has no answer for that beyond SETUP telling
the operator to read its log. Nothing recursive fixes it — a watcher for the
watcher inherits the problem — so the honest statement is that this closes the
degradation gap and not the "is anything running at all" gap. The report's
`generated_at` is the field a later consumer (the operator-inbox digest) can
use to notice the check itself went quiet, and it is there for that reason.
