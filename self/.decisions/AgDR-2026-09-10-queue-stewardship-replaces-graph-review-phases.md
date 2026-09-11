# AgDR-2026-09-10 — Queue stewardship replaces graph-review Phases 2 and 3

- **Status:** proposed (operator ratifies at Gate C)
- **Issue:** #38 (rewritten as the queue-steward ticket); #39 (closed as not
  planned, absorbed)
- **Date:** 2026-09-10
- **Supersedes / amends:** re-scopes Phases 2 and 3 of
  `self/.switchboard/intents/graph-review.md` and the phase plan in `AgDR-012`;
  makes concrete the "per-project queue-steward ritual" that `AgDR-048` §4
  named without specifying.

## Context

graph-review was designed in July 2026 as three phases: a proposals-only
analyzer (Phase 1, #37, merged as `graph_review.py`), an act layer with a small
automatic class plus an in-session `/graph-review` actioner (Phase 2, #38), and
churn-triggered scheduling with a push notification (Phase 3, #39). Phase 1
shipped and has not changed. Phases 2 and 3 never started, and a 2026-08-28
audit found each of their premises gone:

- The `/graph-review` in-session actioner assumed a command surface for
  applying accepted proposals. The fold loop (AgDR-034/035) is that surface
  now: an operator 👍 or `/fold` on a comment, applied headlessly by the
  scheduler's poll with marker-first idempotency. A second approval channel
  would be a second control plane for the same act.
- The automatic reversible class needed `stateReason` in the board query (to
  tell closed-completed from closed-not-planned) and an orchestrator-side edge
  write. Neither exists, and the dispatch gate already treats every closed
  blocker as satisfied — a stale edge is inert. Building the plumbing would buy
  an automatic mutation the system has no need for.
- Churn-triggered scheduling needed a durable cursor. The orchestrator keeps
  all state in-process; a cursor file would be its first on-disk state, added
  for a feature whose trigger ("N issues opened or closed") nobody had asked
  for since July.
- "Push notification" had no substrate. The only operator-facing channels are
  GitHub comments and, since 2026-09-03, the inbox digest — a body rewritten
  in place that, by its own record, notifies nobody.

Meanwhile, between 2026-08-28 and 2026-09-06, the operator performed a
recurring job by hand across several sessions: re-verify open ticket bodies
against `origin/main`, refresh stale `file:line` citations and invalidated
premises through fold proposals, add and audit native `blockedBy` edges, judge
readiness, activate tickets in a stated order, and sweep label vocabulary.
Four tickets rewritten in that window decayed within days as the merge train
(#182–#191, #196–#203) moved main. The work was real, bounded, and recurring,
and it had no owner. `AgDR-048` §4 had already named it — "a per-project
queue-steward ritual" — as one of the three investments that compound when the
scaling axis is projects.

On 2026-09-10 the operator decided to consolidate #38 and #39 into that ritual.

## Decision

**One ticket, one role: the queue steward.** #38 is rewritten as a
recurring **dispatched session** — not a scheduler tick — with seven duties:
re-verify bodies against HEAD and refresh them via fold proposals; maintain
`blockedBy` edges including body-implied ordering; assess readiness
(READY / NEEDS-SPEC) before activation; order activation with a posted
rationale; sweep label vocabulary; carry the original graph-edge duties
(stale-edge and promotable detection) as one duty among these; and run on a
cadence rule. #39 is closed as not planned and absorbed.

Three things this fixes in place:

1. **The auto class is bound to the `board_sanity` bar** — *only what no
   stance could make legitimate; detect, never revert.* The steward reports
   stale and promotable edges; it clears nothing and relabels nothing on that
   evidence alone. Promotable feeds the readiness assessment; it does not skip
   it. The original Phase 2 would have cleared edges mechanically; this record
   says the system already tolerates them and the record of an edge is worth
   more than a tidy graph.
2. **The body write stays with the fold loop.** The steward never edits a
   ticket body directly. It posts a `## Triage verdict`-shaped comment with a
   `body-sha1:` line and a `<!-- fold:proposal -->` block, and the operator's
   approval applies it through the existing `apply_fold_signal` path. One
   write authority for bodies, unchanged from AgDR-035.
3. **#39's mechanism becomes a cadence rule, its notification becomes the
   rationale, and its cursor becomes a comment timestamp.** The steward is due
   after N merges to main (N = 10) or 7 days, whichever first. Its rationale is
   posted as a comment on the "Switchboard operator inbox" issue, which rides
   GitHub's own notification — the thing the digest body cannot do — and the
   timestamp of that comment is the cursor. GitHub is the durable store; the
   orchestrator gains no state file.

The Phase 1 analyzer is untouched: still manually invoked, still proposals-only,
available to the steward as an evidence tool.

**How the steward is invoked is left open on the ticket**, with a
recommendation: operator-invoked, with the cadence rule as a condition the
steward checks and reports rather than a trigger that fires it.

## Rejected alternatives (steelmanned)

- **Build Phase 2 as written, minus the actioner.** The strongest version:
  `stateReason` is one field on an existing query, the REST dependencies
  endpoint is already called by `new-ticket.sh`, and the automatic class was
  always narrow. Rejected because the automatic class's one real act — clearing
  an edge to a closed blocker — has no beneficiary: the dispatch gate ignores
  closed blockers, so the edge is inert, and clearing it destroys the only
  record that the dependency was ever asserted. An automation whose output
  nobody consumes is a maintenance surface with no return.
- **Keep #39 open as the eventual automation of the steward.** Honest about
  the direction, and the churn trigger is a sensible cadence. Rejected because
  it keeps alive the two things the audit found unfounded — a durable cursor
  store in the orchestrator and a notification substrate — for a session whose
  every output needs an operator's approval anyway. A scheduled steward with
  nobody present accrues unread proposals; the cadence rule captures what #39
  was for without the machinery. If unattended runs are ever wanted, the
  precedent is `fleet-health.sh` under launchd, not a scheduler tick.
- **Fold the duties into the triage verifier.** The verifier already
  re-verifies citations, already produces fold proposals, and already runs
  per-ticket. Rejected because the verifier is per-ticket and adversarial; the
  steward is cross-ticket and custodial. Ordering, edges between tickets,
  label-vocabulary sweeps, and "which of these is worth a verifier session"
  have no home in a session that sees one ticket. Merging them would make the
  verifier's one-ticket scope, which is what keeps its verdicts sharp, the
  first casualty.
- **Close #38 too and rely on the hand-run habit.** The cheapest option and
  the current reality. Rejected because a habit with no ticket has no
  acceptance criteria, no cadence, and no rationale trail — the four-day decay
  of rewritten tickets is what the habit already produced.

## Blast radius

- **Tickets:** #38 retitled and rewritten, stays at `status:drafting`, not
  activated. #39 closed as not planned; its native `blockedBy` edge to #38 is
  left in place as the record. No other ticket changes state.
- **Intent and records:** `self/.switchboard/intents/graph-review.md`'s
  three-phase plan and `AgDR-012`'s phase gating are historical for Phases 2
  and 3. Phase 1 and its binding constraints (read `blockedBy` only, never
  `trackedIssues`; proposals-only; one ledger issue) are unchanged and are
  inherited by the steward.
- **Code:** none. `graph_review.py`, `fold.py`, `fold_apply.py`,
  `board_sanity.py`, `inbox_digest.py`, and the scheduler are not touched by
  this decision. The steward's future artifact is a prompt or skill, and a
  session that uses `gh` as the operator login.
- **Inbox issue:** gains a second writer class — steward rationale comments
  from the operator login, alongside the orchestrator's body rewrites. The
  digest record's invariant ("the digest writes no comment") is about the
  digest module and is unaffected; nothing in the orchestrator reads that
  issue's comments.
- **`AgDR-048`:** §4's named-but-unspecified ritual now has a ticket and a
  shape. §1's control-surface claim gains a case: the steward's activation
  relabel is the existing `drafting → triage, actor: human` edge, performed by
  the operator's session.

## Weakest point

**A dispatched session with a cadence held by memory is the same accepted
risk `AgDR-048` already carries, repeated.** "One orchestrator per repo" is
enforced by nothing; "the steward runs every ten merges" is enforced by
nothing either. The 2026-08-29 incident — ten PRs unnoticed for six hours — is
direct evidence that memory-held cadences lapse, and this record adds a second
one. The recommendation on the ticket (operator-invoked) chooses finishability
over guaranteed occurrence, and if the steward is simply not run for a month,
the backlog decays exactly as before and nothing reports it.

The cheap hardening is one read-only line in the inbox digest — merges to main
since the last steward rationale — computed from data the digest already
fetches. It is deliberately not decided here, because it makes the digest a
consumer of the inbox issue's own comments, which is a new coupling the digest
record explicitly avoided. **The prediction to re-read:** if the first three
steward runs are each more than two weeks apart, the invocation answer is
wrong, and the fix is the launchd-dispatched variant (with proposals accruing
for the operator's next session), not a scheduler tick.

Second: the steward's proposals are `## Triage verdict`-shaped comments
authored by the operator login, indistinguishable from verifier verdicts to
the fold poll. That is what makes them apply with zero new code, and it is
also what makes them apply if the operator reacts 👍 to the wrong comment. The
binding rules in `fold.py` (explicit `/fold` with `body-sha1:` outranks a
reaction) are the mitigation; they were designed for one verdict per round,
not for a steward that may post proposals on several tickets in one pass.

## References

- Issues #37 (Phase 1, merged), #38 (this ticket, rewritten), #39 (closed,
  absorbed), #106 (repository-scoped references, closed), #126 (fold apply),
  #192 (inbox digest), #193 (fleet health).
- `AgDR-012` (proposals-only before mutation), `AgDR-034`/`AgDR-035` (fold
  detect / apply), `AgDR-039` (per-project stance ladder — why the auto class
  is bound to a stance-safety bar), `AgDR-045` (gate states are declared),
  `AgDR-048` (single-operator, session-mediated control surface, §4 the
  steward ritual), `AgDR-2026-09-03-the-inbox-digest-is-a-snapshot-not-a-feed`
  (why the digest body notifies nobody and why a comment on that issue is
  safe).
- `orchestrator/src/orchestrator/board_sanity.py` module docstring (the bar:
  detect, never revert; only what no stance could make legitimate).
- Oracle bank: OBS-022 (2026-07-02, comment→`updatedAt` self-unpark — the
  reason the rationale comment lives on the inbox issue and not on a tracked
  ticket).
