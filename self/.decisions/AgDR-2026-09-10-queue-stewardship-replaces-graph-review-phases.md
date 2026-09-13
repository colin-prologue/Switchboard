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
re-verify `status:drafting` bodies against HEAD and refresh them via fold
proposals — the only state `apply_fold_signal` actually writes to;
`FOLD_POLL_STATES` also watches `status:decision`, but `apply_fold_signal`
refuses it outright (`skipped_decision_state`, zero writes — folding an
unanswered decision is deliberately illegal), so a stale citation on a
`status:decision`, `todo`, `in progress`, or `human-review` ticket is named
in the rationale as advisory only, with no fold write path for any of them;
maintain `blockedBy` edges including body-implied ordering; assess readiness
(READY / NEEDS-SPEC) before activation; order activation with a posted
rationale; sweep label vocabulary; carry the original graph-edge duties
(stale-edge and promotable detection) as one duty among these; and run on a
cadence rule. #39 is closed as not planned and absorbed.

Five things this fixes in place:

1. **The auto class is bound to the `board_sanity` bar** — *only what no
   stance could make legitimate; detect, never revert.* The steward reports
   stale and promotable edges; it clears nothing and relabels nothing on that
   evidence alone. Promotable feeds the readiness assessment; it does not skip
   it. The original Phase 2 would have cleared edges mechanically; this record
   says the system already tolerates them and the record of an edge is worth
   more than a tidy graph.
2. **The body write stays with the fold loop, and so does activation —
   posted under a distinguishable identity, with a named fallback.** The
   steward never edits a ticket body directly. It posts a `## Triage
   verdict`-shaped comment with a `body-sha1:` line and a `<!-- fold:proposal
   -->` block, and the operator's approval applies it through the existing
   `apply_fold_signal` path — one write authority for bodies, unchanged from
   AgDR-035. Nothing in `detect_fold_signals` requires the *proposal* comment
   to be operator-authored — only the approval (a reaction or `/fold`
   comment) must come from an operator login (`fold.py:196`, command
   channel; `:238`, reaction channel; the `verdicts` list itself at `:172`
   carries no author filter) — so the steward posts under the GitHub App
   identity (`switchboard-agent`, AgDR-009) rather than the operator's own
   login. This is load-bearing for point 3 below: GitHub does not notify an
   account of its own activity, so a rationale comment posted as the
   operator would never reach the operator. Activation is not a second write
   authority layered on top: `apply_fold_signal` has no guard requiring the
   proposal to differ from the current body, so a fold proposal that
   reproduces a READY ticket's body verbatim, once 👍'd, performs the same
   `drafting → triage, actor: fold` edge every other fold performs.
   **Exception:** a ticket whose current body already contains a literal
   fold-sentinel string — this ticket is itself an example — cannot receive
   a no-op activation fold: `WORKFLOW.base.md`'s own hard rule 3 requires
   *omitting* the proposal block when the body quotes either sentinel, to
   avoid a truncated or malformed payload. For that narrow class the steward
   names it in the rationale, and the existing `actor: human` manual relabel
   is the fallback — no new mechanism is added for it. **A second collision,
   found the same way: a no-op activation comment carries the verdict
   heading but no verdict class, and once it relabels the ticket to
   `status:triage`, it becomes the comment the *next* triage session's own
   Step 0 fast path reads as "the most recent verdict."** That fast path
   previously had no row for a verdict-shaped comment with no stated class —
   every no-op activation would have hit an unhandled case in someone else's
   prompt. Fixed directly, not deferred: `workflow/WORKFLOW.base.md` (mirrored
   to `projects/switchboard-self/WORKFLOW.md`) gained one row routing that
   case to a full review, the same posture as the existing missing-hash
   fallback. This is the one `orchestrator/tests` change and the one shared
   prompt-file change this record makes; see Blast radius. Duty 3 (readiness)
   and duty 4 (activation) are judgment and ordering, not a separate write;
   the write is duty 1's.
3. **#39's mechanism becomes a cadence rule, and its notification becomes a
   genuinely delivered comment, not a pull surface.** The steward is due
   after N merges to main (N = 10) or 7 days, whichever first, and its
   cursor is the timestamp of its last rationale comment — GitHub is the
   durable store, no orchestrator state file. The rationale is posted as a
   comment on the "Switchboard operator inbox" issue and opens with an
   explicit `@<operator-login>` mention (the login already lives in
   `FoldConfig.operator_logins`) — GitHub notifies a directly-mentioned user
   regardless of watch or subscription state, which is the more bulletproof
   half of point 2's identity fix, not a substitute for it: a self-authored
   mention still generates no notification, so both the App identity and the
   explicit mention are required together.
4. **Most of the steward's duties don't require the operator present — the
   write-gated ones, specifically.** Every write that mutates a ticket's
   body or its activation state goes through the fold-approval gate above,
   and none of those need the operator in a session. Duty 2 (`blockedBy`
   edge maintenance) is the named exception: it is a direct, unilateral
   write via the REST dependencies endpoint, ungated by any operator
   reaction — the same pattern `scripts/new-ticket.sh --blocked-by` already
   uses today without approval. This is accepted, not overlooked: a
   mechanical edge write triggered only by prose already present in the
   ticket body carries far less blast radius than a body rewrite or an
   activation, and the precedent for running it unattended already exists.
   Because the write-gated duties don't need the operator present and duty 2
   never did, the steward's own dispatch doesn't either. It is dispatched by
   an external cron/launchd job evaluating duty 7's cadence rule — precedent
   `fleet-health.sh`, never `scheduler.py`'s poll loop — and the operator's
   participation is reading the rationale and reacting to the proposals in
   it, whenever they next look.
5. **The per-cycle cap is a binding rule, not weakest-point commentary.** No
   more than K = 5 fold proposals are posted in one cycle, selected by the
   posted activation order. A READY ticket beyond the cap is named READY in
   the rationale and carries no proposal that cycle; it is prioritized
   *first* in the next cycle's selection, ahead of newly-READY tickets, so a
   steady stream of new arrivals cannot perpetually bump a deferred ticket.

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
  the direction, and the churn trigger is a sensible cadence. Rejected — but
  not because unattended dispatch itself is unwanted; this record ultimately
  chooses exactly that (Decision point 4). What's rejected is #39's specific
  *mechanism*: an orchestrator-owned churn trigger backed by a durable cursor
  store and scheduler-tick wiring, both of which the audit found unfounded and
  neither of which this record needs — the cadence rule is met by a comment
  timestamp, and dispatch is an external cron/launchd job, precedent
  `fleet-health.sh`, never `scheduler.py`'s poll loop. The distinction is who
  owns the schedule: #39 wired it into the orchestrator process; this record
  keeps it entirely outside, so "unattended" here never means "a new
  always-on component," only "a periodic external invocation of a session
  that was already designed to run without the operator's presence."
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
  and 3. Only Phase 1's *read* semantics are inherited unchanged (`blockedBy`
  only, never `trackedIssues`). Its write constraint is explicitly **not**
  inherited: the intent's "proposals-only... exactly one artifact... NEVER
  edits another ticket's body, labels, edges, or milestones"
  (`self/.switchboard/intents/graph-review.md:37-39`) describes Phase 1
  specifically, and the same document calls mutation "Phase 2+" (`:14`) —
  the steward *is* that layer. It writes to individual ticket bodies (via
  fold), `blockedBy` edges (directly), and the inbox issue (rationale):
  several surfaces, not one ledger, by design.
- **Code:** `orchestrator/src` untouched — `graph_review.py`, `fold.py`,
  `fold_apply.py`, `board_sanity.py`, `inbox_digest.py`, and the scheduler
  are not touched by this decision. One exception, landed with this record:
  `workflow/WORKFLOW.base.md` and `projects/switchboard-self/WORKFLOW.md`
  (a shared prompt file, not orchestrator code) gained one fast-path table
  row so a steward's no-op activation comment — which carries the `##
  Triage verdict` heading but no verdict class — cannot be mistaken by the
  *next* triage session's own Step 0 for a prior verdict it should re-route
  on. Pinned by `orchestrator/tests/test_prompt.py::
  test_fast_path_covers_all_six_table_rows`; full suite (1351 tests) passes.
  The steward's future artifact is otherwise a prompt or skill, and a
  session that authenticates as the GitHub App installation (`AgDR-009`) to
  post as `switchboard-agent` — not the operator's own `gh` login, per
  Decision point 2. Minting and exporting that installation token to the
  session's `gh` is new tooling work for the steward's own launch script; it
  reuses `auth.py`'s existing installation-token provider rather than adding
  orchestrator code, but it is not zero effort, and the earlier framing of
  this ticket understated that.
- **Inbox issue:** gains a second writer class — steward rationale comments
  from the GitHub App identity, opening with an explicit operator mention,
  alongside the orchestrator's body rewrites. Posting under a distinct
  identity is what makes the comment a real notification (Decision point 2);
  the digest record's invariant ("the digest writes no comment") is about
  the digest module and is unaffected; nothing in the orchestrator reads
  that issue's comments.
- **`AgDR-048`:** §4's named-but-unspecified ritual now has a ticket and a
  shape. §1's control-surface claim gains a case, and a sharper one than first
  drafted: the steward's activation is not the `actor: human` edge performed
  in a session, it is the existing `actor: fold` edge, approved by an
  operator's reaction wherever they are. The control surface stays
  session-mediated in the sense that matters — every write traces to an
  operator action — without requiring a session for this one.

## Weakest point

**Scheduled dispatch removes the one thing that used to force a look; the
mitigation is a real notification, not the digest's inherited gap this
record originally claimed.** The first draft named memory-held cadence —
"the steward runs every ten merges" is enforced by nothing — as the central
risk, on the premise that the operator would be starting the session anyway.
That premise is gone: activation now costs a reaction, not a session, and the
recommendation moved to cron/launchd dispatch. A later draft of this section
then claimed the steward's rationale "inherits" the inbox digest's own
accepted notify-nobody gap
(`AgDR-2026-09-03-the-inbox-digest-is-a-snapshot-not-a-feed`). That claim
conflated two different GitHub behaviors and was wrong: the digest's write is
a *body edit*, which never notifies regardless of author; the steward's
rationale is a *comment*, which notifies subscribers — provided it is not
self-authored. Decision point 2 fixes exactly this by posting as the App
identity rather than the operator's own login, and point 3 adds an explicit
`@`-mention, which notifies the operator even without a prior subscription.
The residual risk is narrower than either earlier draft stated: it is not
that no notification path exists, and not a memory-held cadence — a
cron/launchd job enforces that mechanically, the way `fleet-health.sh`
already does. It is the ordinary human gap between "was notified" and "acted
on it," compounded by whatever GitHub notification filtering the operator
has configured (muted repos, digest-only email, a filtered inbox) that this
record has no visibility into. The 2026-08-29 incident (ten PRs unnoticed for
six hours) happened under the fully manual system and involved no
notification at all, so it is weak evidence for this specific residual — but
it is the only precedent this record has, and the manual system did have one
forcing function this one removes: starting the steward used to require the
operator to be there. **The prediction to re-read:** if a steward rationale
sits un-reacted-to for more than the 7-day cadence ceiling on three separate
cycles *despite* the mention and identity fix landing correctly, the gap is
attention or notification filtering, not delivery, and the fix is on the
operator's notification settings or an escalating channel — not a return to
operator-invoked dispatch, and not another comment-based mechanism.

Second: the steward's proposals are `## Triage verdict`-shaped comments
authored by the App identity (`switchboard-agent`) — the same identity every
orchestrator-dispatched agent turn already posts under (`AgDR-009` Decision
1), so this is not a new class of comment to the fold poll, just a new
caller of it. `is_verdict_comment` checks the heading only, not authorship
(`fold.py:116-119`), so the steward's proposals are indistinguishable from
verifier verdicts. That is what makes them apply with zero new code, and it is
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
