# AgDR-2026-09-04-the-escalation-reason-is-recorded-not-inferred

## Context

The `prototype` stance's QA role fails closed on a missing cross-model review:
if the configured review bot has not reviewed the current head sha — absent,
stale, or still pending — the session must not SHIP, so it relabels
`status:review → status:human-review` and hands the ticket to a human. That is
correct, and it is the only sound thing to do at the moment of the check.

It was also one-way. The bot typically posts minutes later, often clean. A clean
review opens **no threads**, and the only automation that looked at review state
afterwards — the review-response sub-poll — selects threads through
`needs_response`. No threads means nothing owed, and the sub-poll returned. The
ticket sat at a human gate waiting for a relabel that automation had promised to
make unnecessary. Recoverable only by an operator, and only if they noticed.

The exposure is narrow — a project must compose a QA role on `status:review`
*and* configure `review_response.bot_logins`, which today is civ-life alone —
but the strand is permanent, and "the operator will notice" is exactly the
remedy the methodology says fails, because it depends on remembering.

What issue #198 did not settle, and what this record settles, is **how a
returning ticket is distinguished from every other ticket at the same label**.

## Decision

**The reason a ticket entered the human gate is written down at escalation
time, by the session that escalates. The return path keys on that record, never
on the label.**

Concretely:

1. **A durable marker comment on the PR.** The QA session posts
   `<!-- switchboard:escalated-pending-review sha=<head> bot=<login> -->` as a
   comment's first line *before* the relabel. Third instance of the house
   marker mechanism (`switchboard:response-round`, `CAP_MARKER`,
   `RELABEL_CAP_MARKER`), with the same two authenticity rules: it counts only
   when authored by the normalized App identity, and only on the first line.
2. **No marker, no requeue.** An operator relabel, a gated-stance handoff, an
   escalation-list judgment, and a finding that survived two rounds all land on
   the same label and are all left alone. A missing or malformed marker
   degrades to exactly today's behaviour, which the operator inbox digest
   (#192) already enumerates.
3. **Clean means two shapes, not one.** A bot's clean verdict is EITHER a formal
   review whose state is `APPROVED` or `COMMENTED`, OR a bare 👍 on the PR.
   Both requeue. `CHANGES_REQUESTED` never does, and a 👎 from the same bot
   after the escalation vetoes both.
4. **Cleanliness also requires no unresolved thread from that bot** — strictly
   stronger than `not needs_response(...)`, which passes over a thread
   Switchboard has already answered. The stance treats an unresolved thread as
   unfinished work.
5. **Each shape is pinned to the head sha in the only way its shape allows.** A
   formal review carries `commit { oid }` and is compared directly. A reaction
   carries no commit at all, so it is pinned *indirectly*: the marker's sha must
   still be the head sha, and the reaction must postdate the marker. With no
   marker timestamp the reaction shape is refused rather than guessed at.
6. **One requeue per head sha**, bounded by a receipt comment the sub-poll
   writes before it relabels. Without it, a QA session that escalates again on
   the same commit against the same clean review is requeued onto it forever.
7. **The requeue records its own write** in `_last_status_state`, so
   `_observe_human_relabels` cannot read the orchestrator's edge as a human's
   and mint a session budget nobody asked for.
8. **Gated stances are refused first, before any read.** Where `handoff_label`
   resolves outside `active_states`, `status:review` is a state nothing
   dispatches, and requeueing into it would strand the ticket somewhere strictly
   worse than the gate it already sits at. Checked before the marker fetch, so
   every `base` project — switchboard-self included — spends zero extra API
   calls on this path.

Point 3 is the one the ticket flagged as the way to get this wrong, and it is
load-bearing. Keying on `APPROVED` alone, or on formal reviews alone, would
re-create the indefinite wait one layer down for every bot whose clean signal is
a reaction or an unapproved summary — a bug strictly harder to see than the one
being fixed, because the ticket would look correctly parked.

## Rejected options, steelmanned

**Infer the reason instead of recording it.** The strongest version: everything
needed is already on the PR. If the issue is at `human-review`, a bound PR
exists, the configured bot has a clean review of the head sha, and no thread is
open, then whatever put the ticket there, the conditions for QA to proceed now
hold — so requeue and let the QA session re-decide. This needs no prompt change,
no worker cooperation, and cannot be defeated by a botched escalation.

It was rejected because it makes the requeue **unfalsifiable about its own
premise**. An operator who relabels to `human-review` to stop a ticket — the
single most likely human use of that label — would have it silently pulled back
into the dispatch loop by a bot approval they never asked about. The same holds
for a QA escalation on the escalation list: the diff touched something a human
must judge, the bot's approval is irrelevant to that judgment, and the
inference cannot tell the difference. The label is a destination, not a reason,
and no amount of reading the PR recovers a reason nobody wrote down.

The accepted cost is real: the marker is worker-authored, so a session that
skips or malforms it loses the return path. That degrades to today's behaviour
rather than to a wrong one, which is the direction a fail-closed system should
degrade in.

**Have the orchestrator write the marker instead of the worker.** Tempting —
the orchestrator is reliable and the worker is a language model. But the
orchestrator does not perform this relabel and does not know why the QA session
escalated; only the session holds that. Reconstructing it server-side is the
inference option wearing a different hat.

**Require `APPROVED` only.** Simple, unambiguous, and it is what a "clean
review" means in GitHub's own vocabulary. Rejected on the codex-verdict-signals
reality cited in the ticket: the bot in production frequently signals approval
with a reaction or a findings-free `COMMENTED` review. A rule that is clean in
the schema and wrong about the actual reviewer buys nothing.

**Requeue without a per-sha bound.** One fewer write and one fewer read. But
the QA session that escalated may escalate again on the same commit — it is
allowed to, and a session that still will not ship is behaving correctly — and
then the requeue and the escalation ping-pong the label, burning a QA session
per round. The bound makes the return path a one-time offer per commit, which
is the shape a genuinely new commit still gets afresh.

## Blast radius

- **civ-life only, today.** The four refusals are ordered so a project that does
  not compose a QA role on `status:review` never reaches a new code path, and a
  gated stance exits before the first extra API call. switchboard-self's own
  `human-review` park is untouched by construction, and there is a test for it.
- **Two new tracker reads**, `fetch_pr_reviews` and `fetch_pr_reactions`. Both
  are read-only, both paginate fail-loud like their siblings, and both are
  reached only after a valid marker for the current head sha — so the steady
  state for an unaffected project is zero additional calls.
- **The prompt contract widens.** `WORKFLOW.prototype.md` now specifies a
  marker the runtime parses, so the regex and the prompt are a two-sided
  contract: changing either alone silently disables the return path. This is the
  same coupling `switchboard:response-round` already carries.
- **The findings path is unchanged.** The requeue lives entirely in the
  `if not owed` branch that previously returned False.
- **The declared surfaces move too, and one of them is not inert.** The edge is
  recorded in `workflow/transitions.yml` and in METHODOLOGY's writers table —
  this is the second orchestrator-written edge out of `human-review`, and an
  orchestrator transition living only in code is the drift those surfaces exist
  to prevent. But `edges` is *read*: `status_board.honored_drags` derives the
  honored board-drag set from it, and the generic filter would have honored the
  new row — `human review` is a legal drag source, and `review` is absent from
  the active set that derivation reads (the KNOWN GAP: `load_active_states`
  reads `WORKFLOW.base.md` unconditionally). So `review` joins
  `EXCLUDED_TO_EXTRA`, on the argument already made there for `human review`:
  it is the other `handoff_label`, written only after evidence validation, and
  never by a drag. The honored set stays at three.

## Weakest point

**The marker is written by the least reliable actor in the system, at the
moment it is least supervised.** The escalation relabel is the QA session's
terminal act; if the session posts the marker malformed, posts it after the
relabel and dies, or reasons its way out of posting it at all, the ticket
strands exactly as it does today and nothing reports that the return path was
supposed to fire. The failure is silent and looks identical to "the bot never
reviewed".

What would make this wrong: a run of stranded civ-life tickets whose PRs carry a
pending-review escalation in prose but no parseable marker. That is checkable —
the markers are grep-able by construction — and the fix if it fires is not more
prompt text but moving the write to a place the worker cannot skip.

The second weakest point is narrower and worth naming: **`COMMENTED` is a guess
about a bot's vocabulary, not a contract.** If the review bot ever posts a
`COMMENTED` review carrying findings in its *body* rather than as threads, this
requeues a ticket whose review was not clean, and the QA session would then SHIP
on a review nobody read. The unresolved-thread conjunct is what stands between
that and a bad merge, and it only helps if the bot files findings as threads.
