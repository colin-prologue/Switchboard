# AgDR-010: `status:in-progress` as claim-visibility label (not a lock)

- **Status:** accepted (2026-07-06). Extends AgDR-008 (durable park label) to the
  full claim lifecycle. Implements issue #14.
- **Context:** The orchestrator's claim state lived only in an in-memory set
  (`scheduler.py` `self.claimed`); the sole status writes it performed were park
  labels. A human scanning the GitHub board could not tell which `todo` issues a
  worker currently held. The `status:in-progress` label already existed and the
  tracker already *normalized* it on read (→ active state `"in progress"`), but
  nothing applied it. Related: #12 (makes GitHub viable as the dashboard's data
  source).
- **Decision:** The orchestrator applies `status:in-progress` when it first
  claims a `todo` issue and clears it when the claim genuinely dies. The label
  tracks the **claim, not the session** — one issue receives many sessions
  (continuations, budget/max-turns breaks, failure retries with backoff), and
  between-session backoff windows still mean "a worker holds this," so they write
  nothing. Transitions: first `todo` dispatch swaps `todo → in-progress` (once);
  `_park` removes `in-progress` alongside adding `parked`; a mid-run claim
  release reverts to `todo` + one-line comment; a new **startup sweep** reverts
  claims stranded by a crash, comment-free. New `tracker.remove_labels`
  (`removeLabelsFromLabelable`) mirrors `add_labels`.

## Framing: visibility only, NOT a lock (deliberately)

A label cannot compare-and-swap — GitHub offers no atomic claim primitive — so
this must not pretend to be one. Cross-runner mutual exclusion is a separate,
deliberately-excluded concern (**issue #15**). Everything below is board
accuracy, never liveness: `"in progress"` is itself an active state, so
dispatch/retry never depended on the label.

## The decisions (numbered — code references #3 and #5 by number)

1. **updatedAt echo needs no guard.** No decision path consumes `issue.updated_at`
   anymore (AgDR-008 removed the OBS-022 re-fetch machinery; the park gate keys
   on the `status:parked` label). A label write's `updatedAt` echo therefore
   perturbs nothing. Revisit if a future `updated_at`-sensitive consumer lands.
2. **Ownership split — four writers** (documented in METHODOLOGY.md §"Who writes
   which status label"): humans own gate labels (`drafting`, `plan-review`,
   `blocked`); the triage **verifier agent** owns `triage → todo|drafting`;
   **worker agents** apply `status:human-review` at handoff; the **orchestrator**
   owns `todo → in-progress`, its reverts, and `parked`. A handoff is *observed,
   never reverted*: any status label other than a sole `status:in-progress` means
   someone already moved the issue, so the revert helper leaves it alone.
3. **Role-pin coupling (highest-risk part).** `_worker` captures `dispatch_state`
   at session start and ends the session on ANY between-turn state change
   (role-pinned sessions, AgDR-005 / SPEC.md §4). If `_dispatch` wrote the label
   but left the in-memory `issue.state` as `"todo"`, the turn-1 refresh would read
   back `"in progress"` and force a one-turn break — **burning one of
   `max_sessions_per_issue` (3) on every `todo` dispatch** and making premature
   parking materially likelier. Therefore `_apply_in_progress_label` also updates
   the in-memory `issue.state`/`issue.labels` to the post-write state so the
   orchestrator's own write cannot trip the role-pin check. Pinned by
   `test_todo_dispatch_label_write_costs_no_session` (asserts session parity
   between a `todo` and an `in progress` dispatch — the write-count AC alone would
   not catch this, since the forced break still writes exactly once).
4. **Config caveat (documentation, not code).** Under this repo's config
   eligibility is `_should_dispatch` with `required_labels: []` and `"in progress"`
   an active state, so the self-applied label keeps the issue eligible on the
   retry path — no "still-mine" special-casing needed. A config setting
   `required_labels: ["status:todo"]` WOULD self-release on its own write; that
   combination is noted as unsupported in METHODOLOGY.md rather than defended with
   dead code.
5. **Restart flap — silent startup revert.** On restart the sweep reverts every
   stranded `in-progress` issue and the next tick may immediately re-dispatch it.
   The label may thus flip `in-progress → todo → in-progress` across a restart.
   Accepted, because `in-progress` is an active state so liveness never depended
   on the revert — the sweep is *purely* board accuracy for the stranded window.
   The startup sweep posts **no comment** (a "nobody's working this" note seconds
   before the same runner resumes would be noise and a momentary lie); the mid-run
   claim-release revert keeps its one-line comment because a live release is a
   real signal a human may care about.

## Rejected alternatives (steelmanned)

- **Per-session labeling (write/clear each session).** Simplest mental model:
  label = "a session is live." Rejected — it flaps the label through every
  between-session backoff window, when the claim is still genuinely held. The
  board would strobe `in-progress ↔ todo` on retries, exactly the noise #14
  forbids. The claim, not the session, is the human-meaningful unit.
- **Revert to `todo` between retries.** A cleaner invariant ("no label unless a
  turn is executing right now"). Rejected for the same reason: a claim in backoff
  is still held; reverting invites a peon (or the next tick) to re-grab and
  double-dispatch, and floods the board with transient flips.
- **Make it a real lock.** Tempting to gate dispatch on label-absence for crude
  mutual exclusion. Rejected on principle — GitHub has no compare-and-swap, so
  the "lock" would have a read-modify-write race that silently double-dispatches.
  Pretending a label is a lock is worse than an honest visibility marker. #15.
- **Comment on the startup sweep too (symmetry).** Rejected per decision #5 — a
  restart re-dispatch would immediately contradict the comment.

## Blast radius

`tracker.py` (+`remove_labels`, +mutation constant), `scheduler.py` (dispatch
swap, park clear, shared revert helper, startup sweep), and METHODOLOGY.md (the
four-writer table + config caveat). No changes to human/verifier/worker-owned
labels, to normalization, or to the park gate. The tracker method is a sanctioned
exception to the §11.5 no-writes boundary, like `add_labels`.

## Weakest point (accepted)

**Single-runner-per-repo** is load-bearing for the startup sweep's *destructive*
revert: a fresh process has empty claim state and cannot distinguish "stranded by
my crash" from "held by a live peer," so under multi-runner it would wrongly
revert a peer's claim. Mutual exclusion is out of scope (#15); the sweep and the
`required_labels: []` assumption both carry an explicit "re-gate if multi-runner
lands" note in METHODOLOGY.md. Secondary residual (inherited from AgDR-008): the
per-issue session counter is still in-memory, so a restart mid-issue-pre-park
re-grants a fresh cap — unchanged by this AgDR.
