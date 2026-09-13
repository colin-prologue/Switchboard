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
2. **The body write stays with the fold loop, and so does activation.** The
   steward never edits a ticket body directly. It posts a `## Triage
   verdict`-shaped comment with a `body-sha1:` line and a `<!-- fold:proposal
   -->` block, and the operator's approval applies it through the existing
   `apply_fold_signal` path — one write authority for bodies, unchanged from
   AgDR-035. Activation is not a second write authority layered on top:
   `apply_fold_signal` has no guard requiring the proposal to differ from the
   current body, so a fold proposal that reproduces a READY ticket's body
   verbatim, once 👍'd, performs the same `drafting → triage, actor: fold`
   edge every other fold performs. Duty 3 (readiness) and duty 4 (activation)
   are judgment and ordering, not a separate write; the write is duty 1's.
3. **#39's mechanism becomes a cadence rule, its notification becomes the
   rationale, and its cursor becomes a comment timestamp.** The steward is due
   after N merges to main (N = 10) or 7 days, whichever first. Its rationale is
   posted as a comment on the "Switchboard operator inbox" issue, which rides
   GitHub's own notification — the thing the digest body cannot do — and the
   timestamp of that comment is the cursor. GitHub is the durable store; the
   orchestrator gains no state file.
4. **Because activation costs the operator a reaction rather than a session,
   the steward's own dispatch does too.** Nothing in the steward's duties
   requires the operator present once activation no longer does. It is
   dispatched by an external cron/launchd job evaluating duty 7's cadence rule
   — precedent `fleet-health.sh`, never `scheduler.py`'s poll loop — and the
   operator's participation is reading the rationale and reacting to the
   proposals in it, whenever they next look.

The Phase 1 analyzer is untouched: still manually invoked, still proposals-only,
available to the steward as an evidence tool.

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
- **Operator-invoked session performs activation directly, in-session.** This
  ticket's first draft, same day. Steelman: the steward's duties end in
  operator judgment, so a session the operator is present for is the one that
  finishes its own work, and no approval channel needs inventing. Rejected on
  review because it conflated two separate questions — starting the steward,
  and approving what it proposes — and answered both with "the operator must
  be there" when only the second needed an answer at all, and the fold loop
  already answers it. Requiring a live session for activation when activation
  is just another fold spends the operator's presence on a step that costs
  nothing more than the reaction it already requires for a body correction.

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
  shape. §1's control-surface claim gains a case, and a sharper one than first
  drafted: the steward's activation is not the `actor: human` edge performed
  in a session, it is the existing `actor: fold` edge, approved by an
  operator's reaction wherever they are. The control surface stays
  session-mediated in the sense that matters — every write traces to an
  operator action — without requiring a session for this one.

## Weakest point

**Scheduled dispatch removes the one thing that used to force a look, and
inherits the inbox digest's own accepted gap in exchange.** The first draft of
this record named memory-held cadence — "the steward runs every ten merges" is
enforced by nothing — as the central risk, on the premise that the operator
would be starting the session anyway. That premise is gone: activation now
costs a reaction, not a session, so nothing about running the steward requires
the operator's presence, and the recommendation moved to cron/launchd
dispatch. The residual risk is not that the steward fails to run — a
cron/launchd job enforces the cadence mechanically, the way `fleet-health.sh`
already does — it is that a rationale posted with nobody reading it is
indistinguishable from one that was never posted.
`AgDR-2026-09-03-the-inbox-digest-is-a-snapshot-not-a-feed` already accepted
this exact gap for the digest itself ("the digest notifies nobody... it is
still a pull surface"), and a steward rationale on that same issue inherits it
outright. The 2026-08-29 incident (ten PRs unnoticed for six hours) happened
under the fully manual system, which argues this is not a new failure mode —
but the manual system had one forcing function this one removes: starting the
steward used to require the operator to be there in the first place. **The
prediction to re-read:** if a steward rationale sits un-reacted-to for more
than the 7-day cadence ceiling on three separate cycles, the pull-surface gap
is live, not theoretical, and the fix is the same one named for the digest —
a merges/proposals-pending line pushed somewhere the operator already looks —
not a return to operator-invoked dispatch.

Second: the steward's proposals are `## Triage verdict`-shaped comments
authored by the operator login, indistinguishable from verifier verdicts to
the fold poll. That is what makes them apply with zero new code, and it is
also what makes them apply if the operator reacts 👍 to the wrong comment. The
binding rules in `fold.py` (explicit `/fold` with `body-sha1:` outranks a
reaction) are the mitigation; they were designed for one verdict per round,
not for a steward that may post proposals — several of them now doing double
duty as activations — on several tickets in one pass. Reusing the fold channel
for activation raises this risk's stakes without changing its mechanics: a
misdirected 👍 now moves a ticket to `status:triage`, not just corrects a
citation. A per-cycle cap (K = 5, added 2026-09-13) narrows this by bounding
how many tickets can be in the batch at all — it does not touch the
mechanics above; a misdirected 👍 within a capped batch is exactly as
consequential as before.

Third, added 2026-09-13 from a cross-ticket review: the second paragraph's
"several proposals in one pass" risk is sharpened by #209
(`status:drafting`), filed specifically because triage-verdict and PR-body
comments had gotten too dense to parse reliably — three rounds of triage on
#12 alone. A cadence-dispatched steward turns dispatch into "react to a
comment" at the exact moment reading bandwidth is the known-scarce resource;
the per-cycle cap answers the *volume* of that risk, not the *density* of
each proposal, which #209 is separately trying to fix. #209 does not, as
scoped, touch a steward that is not code yet, so #38 now carries a native
`blockedBy` edge to it — gating this ticket's own implementation dispatch,
not the invocation design decided here, on a leaner convention existing to
build against. Gating the *design* itself on #209 was considered and
declined: #209 cannot reach an artifact that has not been authored, so that
would hold up a decision for a reason that only bears on its eventual build.

## References

- Issues #37 (Phase 1, merged), #38 (this ticket, rewritten), #39 (closed,
  absorbed), #106 (repository-scoped references, closed), #126 (fold apply),
  #192 (inbox digest), #193 (fleet health), #209 (collapse-the-evidence
  readability fix; #38's native `blockedBy` dependency, added 2026-09-13).
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
