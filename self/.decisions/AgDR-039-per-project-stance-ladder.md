# AgDR-039 — Discipline is a per-project stance, not a property of the pipeline

**Status:** accepted
**Date:** 2026-08-15
**Amends:** `AgDR-028` (orchestrator-owned terminal handoff) — target becomes config
**Relates to:** `AgDR-006` (triage as an active state), `AgDR-011` (dispatch marker guard)

## Decision

A project selects a **stance** — a workflow recipe declaring how much process it
runs under — instead of every project running one pipeline calibrated for
architecture-touching work on a mature codebase.

`SB_WORKFLOW_STANCE` in `project.env` selects a template, resolved
project-local-first (`projects/<slug>/WORKFLOW.<stance>.md`) then from the shared
ladder (`workflow/stances/WORKFLOW.<stance>.md`). The legacy
`SB_WORKFLOW_TEMPLATE` is still read as a fallback, so bindings registered before
this change verify unmodified.

This ships one stance, **prototype**. `harden` and `sustain` are named in the
ladder's documentation but deliberately not written until a real project has run
under `prototype` long enough to say what tightening should mean.

### The dial is `active_states`

A gate in this system is not code — it is a state absent from `active_states`,
so the scheduler walks past it and the ticket sits. That was already true; this
record makes it the intended configuration surface rather than an implementation
detail.

The prototype stance lists `review` as active, so the terminal handoff feeds an
agent QA role that merges, rather than a human queue that parks.

### `handoff_label` becomes config

`AgDR-028` fixed the terminal handoff at `status:human-review`. That target is a
**stance property**, not a runtime invariant: a gated stance hands off to a human
gate, an autonomous one hands off to a QA state it also dispatches.

`tracker.handoff_label` now supplies it, defaulting to `status:human-review` so
every pre-stance binding behaves exactly as before. **AgDR-028's substance is
untouched** — the orchestrator still owns the transition, still validates
evidence freshness, worktree cleanliness, the single open PR, its closing
reference, and the head sha before writing anything. Only the destination is
parameterised.

Pairing constraint: a stance that sets `handoff_label` to a state absent from
`active_states` would park every completed ticket forever. The two are set
together, and a test pins that pairing.

## Why

Issue #14 took four triage rounds. Issue #32 took fourteen, five hours, and seven
session-cap parks before a line of implementation — with round 6, 7, 8 and 13's
verdicts each attributing findings to the *previous round's own fix*. The triage
rubric grew 6 → 22 checks. In the week before this record, review-response commits
outnumbered feature commits.

None of that is a defect in any individual rule. It is what happens when one
calibration is applied to every project at every stage of maturity, and nothing
ever removes a rule. `METHODOLOGY.md` already argues proportionality per ticket
("if you find yourself forcing heavy ceremony onto a five-minute bug, you've
mis-set the entry state"); this extends the same argument to the project.

## What was rejected

**Loosening the shared pipeline.** Simplest, and it would have helped the next
prototype immediately. Rejected because it trades one miscalibration for
another — mature work genuinely wants triage, Gate A, and Gate B, and removing
them globally to serve exploratory work would break the case the pipeline was
built for.

**A numeric strictness setting.** Considered and rejected: strictness is not one
axis. A project can want adversarial ticket verification and agent merge
simultaneously, or neither. A template expresses that; a dial does not.

**Deleting the machinery the prototype stance omits.** The fold subsystem,
`status:decision`, and the triage rubric are *unreferenced* by this stance, not
removed. Deleting ~1,250 lines before a real project has run without them is a
guess; leaving them unreferenced makes the cut list an experiment instead.

## What would make this wrong

**If stances proliferate.** The value is a small ladder every project can be
placed on. If each project ends up with a bespoke local template, this is
per-project configuration with extra ceremony, and a shared base with overrides
would have been the better shape.

**If the prototype stance produces work that has to be redone.** The bet is that
three inline preflight checks plus cross-model QA catch enough on exploratory
work. If prototype-stance output is routinely rewritten once a project hardens,
the checks were cut too far — and the fix is to move checks between stances, not
to abandon the ladder.

Early signal: the first project to run under `prototype` for a fortnight. If
nobody reaches for the omitted machinery, the omissions were right and the
deletions become safe. If the fold loop is missed within days, this record's
scoping was wrong.

## Known gaps at merge

- **`METHODOLOGY.md` does not mention stances.** It still describes one pipeline.
  That documentation pass is deliberately not in this diff — it is prose work
  that deserves its own review, not a rider on a mechanism change.
- **Cross-model QA is designed but unwired.** The prototype stance's header
  records the intent and the three concrete blockers (the strict `providers:`
  envelope rejects the legacy top-level `claude:` block; the Codex path needs a
  host-side login; the mixed-canary rollout review has not happened). Until it
  lands, QA runs on the same model as implementation, which is the stance's main
  known weakness.
- **`#133`'s worker merge-guard would disable this stance's QA role.** It denies
  `gh pr merge` unconditionally. It must read `handoff_label` — allow the merge
  when the handoff target names an active QA state, deny otherwise — or merging
  it silently removes the autonomy this record adds.
