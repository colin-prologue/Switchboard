# Product intent: graph-review

- **Slug:** `graph-review`
- **Status:** active (Phase 1 landing with issue #37).
- **Authored in-session note:** the binding design was expected to arrive on
  branch `docs/v0.2-graph-review-design`. That branch was not reachable from the
  worker (only `main` exists on the remote), so this file and `AgDR-009` were
  authored **in-session** from the issue #37 contract, per the ticket's stated
  fallback ("the intent must be reachable — push the design branch **or
  implement in-session**"). Ratify or overturn at the merge gate.

## What + why

The open ticket board accretes latent structure the tracker does not enforce:
prose says "blocked by #N" but no native edge exists; two tickets are really one;
a milestone is missing or wrong; a merged PR quietly invalidated a ticket's
assumption; a ticket's blockers are all closed so it is promotable. A human
eventually notices these by hand. **graph-review** is the pass that surfaces
them mechanically as evidence-cited proposals a human can accept or dismiss.

The system is delivered in **three phases**, gated on measured proposal quality
(see `AgDR-009`):

1. **Phase 1 (MVP, this intent's initial scope):** a manually-invoked analyzer
   that reads the board and writes evidence-cited, keyed proposals to a single
   rolling **Graph Review** issue. Proposals-only. No graph mutation.
2. **Phase 2:** a `/graph-review` on-demand actioner that applies an accepted
   proposal (adds the edge, sets the milestone, …).
3. **Phase 3:** scheduling / an auto class that runs the analyzer on a cadence.

Phase 1 ships alone deliberately: its purpose is to **measure proposal quality /
false-positive rate against Colin's judgment** before any automation is trusted
to mutate the graph.

## Binding constraints (NFRs / environment / failure policy)

- **Read-only except the ledger.** The analyzer reads the board and writes
  exactly one artifact: the rolling Graph Review issue. It NEVER edits another
  ticket's body, labels, edges, or milestones. (Mutation is Phase 2+.)
- **Native edges via `blockedBy` only.** Dependency edges are read through
  GitHub's `blockedBy` GraphQL connection (`tracker.py:14`) — the same
  relationship the scheduler gates dispatch on. `trackedIssues` /
  `trackedInIssues` (GitHub's task-list hierarchy) is a *different* feature and
  MUST NOT be consulted: reading it yields false "no edge" verdicts. A
  "missing edge" proposal is emitted only when `blockedBy` genuinely lacks it.

  > This overrides the stale "native edge data is readable via
  > `trackedIssues`/`trackedInIssues`" line in the issue's Assumptions block.
  > That assumption is false for our purposes; AC-6 is authoritative.

- **Idempotent by a stable marker.** Exactly one Graph Review issue exists.
  Re-running finds it by an HTML-comment marker in its body and updates it in
  place; it never creates a second.
- **Reads its own prior output.** Every proposal has a stable key
  `category:sorted-issue-list`. A key a human has marked `accepted` or
  `dismissed` on the ledger is NEVER re-raised. The analyzer parses the existing
  ledger before regenerating.
- **Skeptic pass on structural proposals.** merge / split / resequence proposals
  (structural judgment calls) pass a refute sub-check that tries to disprove the
  relationship before being written; mechanical proposals (edge, milestone,
  stale-assumption, promotable) skip it. A refuted structural proposal is
  dropped. If the refuter cannot run, the unproven structural proposal is
  dropped (conservative — Phase 1 optimizes for low false-positive rate).
- **Single board.** switchboard-self only; no cross-project scope.
- **Documented command, no scheduler entry.** The analyzer runs via a documented
  CLI invocation. No `class`/scheduling wiring is added in Phase 1.

## Proposal categories (Phase 1)

| Category           | Key example              | Refuted? | Signal (mechanical unless noted)                                   |
|--------------------|--------------------------|----------|--------------------------------------------------------------------|
| `edge`             | `edge:16,31`             | no       | prose hard-dependency ("blocked by/depends on/requires #N") with no native `blockedBy` edge for the pair. Soft "see also #N" is NOT flagged. |
| `milestone`        | `milestone:29`           | no       | issue has a native blocker that is milestoned but the issue itself has no milestone. |
| `resequence`       | `resequence:16,29`       | **yes**  | a native blocker is scheduled in a *later* milestone than the ticket it blocks. |
| `merge`            | `merge:31,35`            | **yes**  | two open issues with high title overlap that cross-reference each other. |
| `split`            | `split:35`               | **yes**  | one issue enumerating many independent deliverables. |
| `stale-assumption` | `stale-assumption:35`    | no       | a merged PR references the issue with a superseding verb, or references it while the issue states an explicit assumption. |
| `promotable`       | `promotable:20`          | no       | every native blocker of the issue is closed. |

Each proposal record carries: the stable **key**, **cited evidence**
(issue#/comment url/PR SHA), a **suggested action**, and a **state**
(`open` | `accepted` | `dismissed`).

## Non-goals (hard boundaries)

- No auto class, no `/graph-review` actioner, no scheduling (Phases 2–3).
- No cross-project scope; single board.
- Never edits a ticket body, labels, edges, or milestones.
