# AgDR-047 — The fail-review episode is bounded by a durable marker, and the cap branch writes before it resets

**Status:** proposed (ratify or overturn at the #31 merge gate)
**Issue:** #31 — Fail-review #20b: verifier — park-time dispatch, classify, route recovery
**Supersedes / amends:** amends the cap-hit routing AgDR-002/008 established and
#28 refined (cap-hit → `status:parked`). `parked` remains the terminal; it is no
longer the *first* stop for an implement-role cap-hit. Extends AgDR-033's
per-role session budgets with a third role.

## Context

Before this, an issue that exhausted its implementation budget got
`status:parked` and nothing else. The operator arriving at it inherited the
whole diagnosis: read the transcripts, work out whether the session hit a
permission wall, looped, or was simply too big, then decide what to do. That is
homework, and it is the same homework every time.

#31 makes the cap-hit dispatch one fresh, independent session that classifies
*why* the issue failed, cites evidence, and routes recovery — the post-failure
twin of triage. The machinery is deliberately triage's: a label-conditional
prompt mode, an active state, `gh issue edit` routing by the session itself.

Three questions had no precedent and had to be decided here.

## Decision

### 1. The episode loop is bounded by a DURABLE marker, not by in-memory state

A retry-class verdict routes back to `todo` and re-grants the implementation
budget. Without a bound that is a closed loop with no human in it: cap-hit →
reset → verifier → `todo` → cap-hit → repeat. `blockage:permission` is the
canonical repeat offender, because the re-dispatched worker strands on the same
denied command that stopped the last one, and `status:parked` is never reached.

In-memory state cannot carry the bound — a restart empties
`sessions_per_issue`, which is the identical argument that made `PARK_LABEL`
durable in the first place. So the orchestrator writes **`gate:fail-reviewed`**
alongside the status relabel. Present at a later implement cap-hit, the cap
branch calls `_park()` instead of relabelling. Removing the marker re-arms
fail-review; that is the operator affordance, and the park comment now says so.

**One episode is the default**, matching the fail-review session cap of 1.
Making the episode count configurable is a later ticket's decision, not this
one's.

The marker is orchestrator-written rather than session-written on purpose: the
bound must not depend on the agent session having successfully posted anything.

### 2. Writes first, counter reset only after they succeed

The reset clears the implement counter the verdict may route back into. If it
landed *first* and the `status:fail-review` write then failed transiently (5xx,
rate limit), the issue would keep `status:todo` with a freshly cleared implement
budget — a full budget re-granted with no relabel, no verifier and no verdict,
repeating on **every** transient error. That is a worse failure than the one
this feature fixes.

So the order is: status write, marker write, then reset. Every failure branch
therefore runs with the counter still at cap, which is exactly what `_park`'s
own comment already promises. Failure behaviour is decided **per error code**,
not uniformly:

- `github_label_not_found` → `_park()`, **without arming the dispatch halt**.
  `register-project.sh` runs at scaffold time only, so every project registered
  before this ships lacks the label; falling back to today's durable behaviour
  is what makes the "no back-provisioning" non-goal safe. The halt stays
  reserved for a missing `status:parked` — arming it here would stop all
  dispatch runner-wide over one project missing one optional label.
- `handoff_preempted` / `handoff_swap_uncertain` → log, `REFUSED`, touch
  nothing. A newer transition won, or the write is commit-ambiguous.
- `handoff_label_rollback_failed`, and any failure of the marker write → `_park()`.
- anything else → `REFUSED`; the counter is still at cap, so the next tick
  retries.

The ambiguous-swap case is repaired by an **idempotent dispatch guard** rather
than by retrying the write: if the swap landed silently, the next tick arrives
at `fail review` and the cap branch is never re-entered, so a retry story would
be fiction for precisely the branch that matters.

### 3. `_park` strips the two orchestrator-owned claim labels — and backfills

`_park` hard-coded `remove_labels([IN_PROGRESS_LABEL])`. A fail-review cap-out
carries `status:fail-review`, which sorts before `status:parked`, so without a
change every reader would report a parked issue as `fail review`.

The rule is: strip exactly `{status:in-progress, status:fail-review}` — the two
labels the orchestrator owns, which are also exactly the park-reachable labels
that sort before `status:parked` — and if that leaves no non-park `status:*`
label, **add `status:todo` first, then strip**.

The backfill is not tidiness. An issue stripped to `[status:parked]` alone
derives `"none"` the moment the operator removes that label, and `"none"` is in
no `active_states` list — so the issue vanishes from the candidate poll on the
one recovery action the park comment documents. Add-first makes that unreachable
even if the strip then fails.

The strip is deliberately **not** generalized to "every `status:*` except
`status:parked`": `status:todo` and `status:triage` sort *after* `status:parked`,
so leaving them is derivation-safe, and they are what makes the unpark round
trip land somewhere the poll can see.

## Rejected alternatives (steelmanned)

- **Fold fail-review into `VERIFY_STATES` instead of minting a third role.**
  Cheapest possible change, and the verifier genuinely is a verification
  activity. Rejected: sharing triage's counter means arriving at fail-review
  with the verify budget already spent whenever triage used its cap — the
  feature would silently fail to fire on exactly the tickets that needed triage
  most.
- **Orchestrator-side routing out of `fail review`, driven by a
  machine-readable verdict channel.** Strictly more controllable, and the
  `.run/handoff-evidence.json` + `validate_handoff` pattern is a real precedent.
  Rejected: it needs a verdict channel this ticket does not specify, and triage
  already routes itself with `gh issue edit` under the same posture. Matching
  the existing bookend beat inventing a second mechanism.
- **Bound the episode with an in-memory counter.** No label to provision, no
  tracker write to fail. Rejected outright: a restart empties it and the loop
  becomes unbounded — the same reason parking was made durable.
- **Raise the fail-review cap above 1 so the verifier gets a retry.**
  Tempting, because `_refund_issue_session` refunds only on a provider-circuit
  failure, so an `error_max_turns` or a denied-command strand burns the single
  session and the issue parks with no verdict. Rejected as the wrong lever: the
  verifier reads evidence and posts a conclusion, it does not iterate. A second
  pass on the same evidence mostly buys a second chance to strand the same way.
  **Accepted tradeoff, recorded so it is not "fixed" by accident.**
- **Retire the `{todo, in-progress} → parked / cap-hit` edges from the
  transition table.** They look superseded. Rejected: the unprovisioned-label
  fallback performs exactly those transitions, and it is an always-on path for
  every project registered before this ships.

## Blast radius

- **`_park` behaviour changes for every caller, in every project** — it now
  strips a second label and may add `status:todo`. The backfill only fires when
  the strip would leave nothing non-park, which at HEAD is reachable from
  `status:in-progress` alone, so existing parks are affected in exactly the case
  that was already stranding.
- **`REVIEW_CAP_COMMENT`'s stated rationale went stale** and is corrected in the
  same pass. Its conclusion (`human-review` is still not park-safe) stands, for a
  different reason than the one it gave: a gate label the orchestrator does not
  own is not its to clear.
- `workflow/transitions.yml` gains nine edges/annotations. `transitions.py` reads
  only `requires_marker`, so nothing executable changes — but #52's Action reads
  the committed table, so it must enumerate what the implementation can actually
  emit. A test asserts that mechanically rather than by inspection.
- **Two new labels must be provisioned.** `register-project.sh` covers new
  projects; the self-pilot repo was done by hand; already-registered third-party
  projects are covered by the fallback, not by back-provisioning.
- The fail-review prompt mode is a new branch read by **all sessions in all
  projects** — but it is label-conditional on a label almost no issue carries,
  so the cost is one `elsif` miss.

## Weakest point

**The verifier's posture is prose, and prose is not a permission.** "Never write
feature code, never commit, never open a PR" is enforced exactly the way
triage's identical posture is enforced: by asking. A fail-review session that
decides the fix is obvious can write it, and nothing in the tool layer stops it.
The named follow-up — a per-mode PreToolUse deny-list on the #133 merge-guard
mechanism — is real but not in this ticket, so until it lands the guarantee is
behavioural. This is the same exposure AgDR-036 was written about, and that
record's refutation section is the one the 2026-08-15 sweep found had never been
verified at all. Worth watching for the same shape here.

Second: the episode bound assumes the marker survives. Any actor that strips
`gate:fail-reviewed` — a human tidying labels, a future script — silently
re-arms an unbounded retry loop, and nothing logs that it happened. The bound is
durable against restarts, not against label edits.

Third, and narrowest: with `remove_labels` failing, a parked issue keeps
deriving `fail review` rather than `parked`. That is safe — the `PARK_LABEL`
gate reads the label, not the derived state — but it means the board can show a
state the scheduler disagrees with, and the multi-label diagnostic will log it
on every poll until someone repairs the labels by hand.
