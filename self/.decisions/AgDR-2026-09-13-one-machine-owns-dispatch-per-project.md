# AgDR-2026-09-13-one-machine-owns-dispatch-per-project

## Context

Colin stood up Switchboard on WSL on a dedicated desktop PC. Before today, this
Mac was already running `launchd` agents dispatching both registered
projects — `switchboard-self` and `civ-life` — via `com.switchboard.*.plist`.
Both machines carry both project registrations, so from today both machines
were dispatching against the same two GitHub boards at once.

`orchestrator/src/orchestrator/singleton.py` already solves a version of this
problem: `AgDR-042` / issue #130 found two orchestrator *processes* racing
against `switchboard-self` on the same machine — no launcher held a lock, so
every dispatch happened twice (paired verdicts seconds apart, per-role budgets
burning at 2x, mixed park formats, sticky re-parks). The fix was an
`fcntl.flock` on `.run/orchestrator.lock`, keyed on the workflow's parent
directory.

That lock does not reach this case. It rides the local filesystem of one
checkout; a second checkout on a second machine takes its own independent
flock without contention. Two machines, each correctly enforcing "one
orchestrator per checkout", still reproduces the exact AgDR-042 failure mode
across the machine boundary instead of within one machine — the lock's scope
was never the thing that made single-dispatch true, only an accident of both
processes sharing a filesystem until now.

## Decision

**One machine dispatches per registered project. Today, that's the WSL
desktop for both `switchboard-self` and `civ-life`; this Mac carries no
dispatch daemon for either.**

Concretely:

1. Both `com.switchboard.switchboard-self` and `com.switchboard.civ-life`
   LaunchAgents on this Mac were stopped and durably disabled via
   `launchctl disable gui/<uid>/com.switchboard.<slug>` — confirmed via
   `launchctl print-disabled`. A bare `launchctl unload` was tried first and
   rejected: both plists carry `RunAtLoad=true` plus `KeepAlive`, so an unload
   with no `-w`/`disable` survives only until the next login or reboot, at
   which point launchd reloads the plist from disk and starts it again.
2. This Mac keeps filing tickets and writing specs/plans interactively.
   That path is unaffected — it was never routed through the orchestrator
   daemon, and nothing here changes it.
3. Verified live, not just inferred from the code: `switchboard-agent[bot]`
   posted a complete triage verdict on issue #211 and relabeled it
   `status:triage` → `status:drafting` at 19:48:06Z, with this Mac's
   orchestrators already confirmed dead (no process, no `launchctl list`
   entry) and its per-issue `.run.log` files showing zero activity anywhere
   today. WSL was the only live dispatcher that could have produced it.
   Three more tickets (#212, #213, #214) filed after that point were each
   auto-labeled into `status:triage` and carried out to a verdict
   (`status:drafting` / `status:decision`) within minutes, with this Mac
   still fully dark throughout — steady-state confirmation, not one lucky
   verdict.

## Rejected options, steelmanned

**Make the lock span machines** — a lease held in the GitHub project itself
(a lock issue, or a label with an owner and a heartbeat) instead of a local
flock. The strongest version: this removes the manual-convention risk
entirely, both machines could run unattended, and it generalizes to any
future third machine without another conversation like this one.

Rejected for now. It's real infrastructure — heartbeat, staleness detection,
what happens on a network partition, a second failure-mode class layered on
top of the one `singleton.py`'s docstring already spent a paragraph
explaining. With one operator and two machines, the cost of that
infrastructure currently exceeds the cost of a convention that's binary and
cheap to audit (`launchctl print-disabled`, one command, two lines). Revisit
if a third machine, or an unattended multi-operator setup, makes the
convention actually hard to keep straight.

**Split projects across machines** — Mac keeps `civ-life`, WSL takes
`switchboard-self`, or vice versa. The strongest version: no machine sits
idle, both get real dispatch throughput, and there's precedent for
project-level partitioning in how these are registered in the first place.

Rejected because it doesn't match today's registration (`scripts/list-projects.sh`
shows both machines registered for both projects) and trades one binary
invariant for an ongoing one: "these two registrations must never drift back
into overlap" is a fact about *configuration*, silently violable, and exactly
the kind of thing that already went unnoticed once today. "Zero daemons on
this machine" needs no comparison against the other machine's state to
verify; "machine A owns project X" does.

**Do nothing and let downstream gates catch duplicates** — a clean-verify
step, or a human noticing paired PRs, mops up after the fact. The strongest
version: no action needed today, and Switchboard already has fail-review and
verify machinery that exists partly for this.

Rejected because AgDR-042 is the record of this exact failure *not* being
caught cheaply: it cost 2x budget burn and produced state (mixed park
formats, sticky re-parks) that needed manual cleanup, not an automatic one.
Downstream gates catch divergent outcomes; they don't catch two dispatchers
starting from the same `status:todo` label in the same race window, which is
where the actual waste happens.

## Blast radius

- Two LaunchAgents disabled on this Mac (`com.switchboard.switchboard-self`,
  `com.switchboard.civ-life`). No code changed, no schema changed.
- Interactive workflows on this Mac (ticket filing, spec/plan writing,
  triage-by-hand) are untouched — none of them go through the orchestrator.
- `status:triage` is itself a dispatched state (verifier session), not manual
  work — this Mac no longer triages anything for either project. Confirmed
  above that WSL does.
- `singleton.py`'s docstring gets a one-line pointer to this record, so the
  next reader doesn't conclude the flock is sufficient protection against a
  second machine.

## Weakest point

**The disabled bit lives in launchd's override database, not in the plist
file or this repo.** `launchctl print-disabled` is the only place that
truth is visible; reading the plist, or this repo, shows nothing wrong.
A future session that runs `launchctl load`/`bootstrap` on either plist —
reasonably expecting it to start — will get a silent no-op, not an error,
and may conclude dispatch is running on this Mac when it isn't. Re-enable
path, if this Mac ever needs to take dispatch back: `launchctl enable
gui/<uid>/com.switchboard.<slug>` before loading.

**There is still no enforced invariant, only a convention.** Nothing stops a
third machine — or this Mac, re-enabled without this record being read first
— from registering the same projects and reproducing today's overlap. The
lock in `singleton.py` still only protects one filesystem; this record is the
entire mechanism that protects the other case, and it's a paragraph in a
file, not a check that runs.

**What would make this wrong:** if a third machine, or a second operator,
enters this setup and the convention isn't discoverable from the code before
someone starts a daemon. The condition to watch for is the same shape as the
original incident — paired triage verdicts seconds apart, or per-role budget
burn at roughly 2x expected — which is exactly what a re-overlap would
produce again.

## References

- Issue #130 / `AgDR-042` (the same failure mode, one machine, two processes;
  `orchestrator/src/orchestrator/singleton.py`).
- Issue #211 ("Add official WSL user-service deployment profile") — the WSL
  deployment ticket in flight; its own triage verdict at 19:48:06Z today is
  this record's live confirmation.
- `~/.workstream/inbox.md`, "Switchboard + civ-life: single dispatcher per
  project" — the working note this record supersedes; promoted here per its
  own text.
- `scripts/list-projects.sh` — confirmed both machines registered for both
  `switchboard-self` and `civ-life` at decision time.
