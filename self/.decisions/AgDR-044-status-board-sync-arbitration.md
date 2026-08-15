# AgDR-044 — The board's write direction arbitrates by field-value timestamp, and ships unprobed

- **Status:** proposed (ratify or overturn at Gate C)
- **Date:** 2026-08-13
- **Issue:** #22
- **Touches:** `orchestrator/status_board.py`, `.github/workflows/status-board-sync.yml`,
  `workflow/transitions.yml` (first consumer of `edges`), `scheduler.py` (comment)

## Context

Issue #22 makes the Project board writable: dragging a card changes an issue's
`status:*` label. The orchestrator reads those labels to decide what to
dispatch, so the write direction is a control surface for automation-owned
state, and the ticket restricts it to an allowlist derived from the safety
invariants rather than from `transitions.yml`'s `actor` column.

Two things had to be decided in code that the ticket specified in prose, and one
prerequisite turned out to be unmet at implementation time.

## Decision

**1. A divergence is arbitrated on the FIELD VALUE's own `updatedAt`.** A
field/label mismatch has two causes that look identical: a human drag, and a
human label edit whose mirror run has not landed yet. Honoring the second
reverts a human. The poll calls it a drag only when
`ProjectV2ItemFieldSingleSelectValue.updatedAt` is strictly newer than the
issue's last `labeled`/`unlabeled` timeline event. Every ambiguous case —
missing timestamp, unparseable timestamp, no value node at all — resolves to
"the label wins", because a snap-back is recoverable on the next cycle and a
rewritten label is not.

**2. The honored set is derived, never listed.** `honored_drags()` runs the
invariant rule over `transitions.yml` at call time; the three-edge result
(`todo → plan review`, `plan review → drafting`, `decision → drafting`) is
pinned in the tests. A future edge that changes the derived set fails the test
and forces a deliberate re-derivation instead of silently widening what a drag
can do.

**3. The premise probe did not run, and the write direction ships anyway.** The
ticket's preflight — create a scratch single-select field, flip it A→B to prove
`updatedAt` moves, then toggle an inert label to prove an issue-side write does
NOT move it — requires an existing Project. At implementation time
`colin-prologue` owns zero Projects v2 (`projectsV2.totalCount == 0`), so there
was nothing to probe against, and the Projects-touching acceptance criteria
(field creation, the option diff, the backfill) moved to the merge gate per the
ticket's own failure-scope rule. The arbitration RULE is unit-tested; the
PREMISE it rests on is not yet verified against the live API.

## Rejected options, steelmanned

- **The ITEM's `updatedAt`.** One field on a node already fetched, no extra
  query. Rejected: its scope is undocumented and plausibly bumps on linked-issue
  edits — including the label write itself. That would make the comparison a
  tautology returning "drag" in exactly the failed-mirror case the arbitration
  exists to catch, and the failure would be silent.
- **The `creator` / actor of the field value.** Schema-available and it is the
  question one actually wants to ask ("did a human do this?"). Rejected: under
  one personal `PROJECT_SYNC_PAT` the mirror and the drag are the same actor, so
  for a solo operator it cannot discriminate at all.
- **`transitions.yml`'s `actor: human` column as the allowlist.** It reads like
  the right predicate and needs no derivation. Rejected: the column records who
  performs a pipeline transition, and its human rows include every gate exit into
  orchestrator territory (`parked → todo`, `human-review → todo`). It would make
  a drag dispatchable and a drag an unpark.
- **Snap back everything (drop the write direction).** Strictly safer, and the
  ticket names it as the fallback if the probe falsifies the premise. Rejected
  as the default: it reduces the board to the read-only lens the ticket exists
  to replace.
- **An org move, or an external relay, to get a real Projects v2 webhook.**
  Rejected in the ticket: an account migration or standing infrastructure for a
  convenience surface.

## Blast radius

Bounded by the allowlist. No honored edge lands in an active state, touches
`in-progress` on either side, or leaves `parked` — so no drag can dispatch an
issue, kill a running session, unpark, or hand work to Gate C. A wrong
arbitration in the honored direction costs one reverted label edit on a ticket
in a non-active state, visible in the run log and recoverable by re-labelling.
The mirror direction cannot loop: a field write fires no run, and the honored
label write's echo run reads equal values and no-ops.

## Weakest point

Decision 3. The whole honored-drag path rests on a scoping claim about
`ProjectV2ItemFieldSingleSelectValue.updatedAt` that has not been checked
against the real API. If GitHub bumps that timestamp on an issue-side label
write, the arbitration inverts in the worst direction: a label edit whose mirror
has not landed reads as a drag, and if its backwards pair is one of the three
honored edges, the Action reverts the human's edit. The three affected pairs are
enumerated and unit-tested (`plan-review→todo`, `drafting→plan-review`,
`drafting→decision`), so the failure is bounded and diagnosable — but it is a
real failure, and the probe is the thing that would rule it out. Run it at the
merge gate, before the first drag.
