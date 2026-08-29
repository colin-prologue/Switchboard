# AgDR-049 — State precedence is declared, not alphabetical

- **Status:** proposed (ratify at the adopting PR's merge gate)
- **Issue:** #167 part (a)
- **Date:** 2026-08-29
- **Supersedes / amends:** discharges the half of `AgDR-008`'s deferred
  "single-status-column cleanup" that concerns *derivation*. `AgDR-008` said
  "`status:parked` is applied additively (the prior `status:todo` is not
  removed); single-status-column cleanup belongs to #22" — #22 shipped as board
  arbitration (`AgDR-044`) and never did that cleanup, and `AgDR-048` retired
  #22's premise entirely, leaving the deferral without an owner. This record
  takes the derivation half. The *namespace* half — moving the hold marker out
  of `status:` — remains open as #167 part (b) and is deliberately not decided
  here.

## Context

`status:*` labels model workflow state. One of them does not: `status:parked` is
an **overlay** — "hold this wherever it is" — and the repo has documented it that
way since it shipped (`README.md`, "additive overlay label", "added *alongside*
the current status, so unparking resumes in the same role"). `_park` goes further
and *backfills* `status:todo` when a strip would leave nothing non-park, because
the residual stage label is the only record of what the ticket resumes into: an
issue stripped to `[status:parked]` alone derives `"none"` on unpark and is
invisible to the candidate poll.

So an issue carrying two `status:*` labels is a **designed steady state** here,
not only a board error, and derivation needs an answer for it.

The answer was `sorted(...)[0]` (`tracker.normalize_status_state`). Alphabetical
order happens to put `status:parked` ahead of `status:todo` and `status:triage`
and **behind** `status:fail-review` and `status:in-progress`. Three consequences
had accumulated by `944af20`:

1. **A held ticket could derive an active state.** `status:in-progress` +
   `status:parked` derived `"in progress"`, which clears the `active_states`
   filter. Dispatch was prevented only by a separate literal `PARK_LABEL` check
   — which `AgDR-008` itself described as "the robust, sort-order-independent
   gate", an admission that the ordering was not robust. `_park` later added a
   best-effort strip of the two offending labels, but the strip is explicitly
   cosmetic and runs *after* the park write, so the hazard moved to the failure
   path and the window between writes rather than closing.
2. **The coincidence became load-bearing in prose.** `PARK_STRIP_LABELS` was
   justified as "exactly the park-reachable labels that sort BEFORE
   `status:parked`"; `transitions.yml` called a retained `status:triage` safe
   because "it sorts after `status:parked`"; the multi-label diagnostic logged
   "using sorted-first". Three comments promising to keep an alphabetical
   accident true, and a fourth park-reachable label (`status:fail-review`,
   #31) whose strip/keep fate was decided by where it happened to sort.
3. **A test recorded the resulting gap as structural.** #31's
   `test_park_backfill_survives_a_failing_strip` documented that its own AC
   ("the post-park set still derives to `parked`") *could not hold*, because the
   stripped labels are by construction the ones that win sorted-first
   derivation. That was an artifact of the ordering rule, not a structural fact.

## Decision

**The winner among several `status:*` labels is declared, in one committed list,
and derivation consults it.**

1. `workflow/transitions.yml` gains a `precedence` section: an ordered list of
   states, highest priority first. It sits beside `requires_marker` as the
   second section the orchestrator reads — both are answerable from a *current*
   label set alone, which is the line `edges` stays on the far side of (cross-
   restart `from` reconstruction is the durability trap `AgDR-008` escaped).

2. `tracker.normalize_status_state` ranks by that list instead of sorting.
   States absent from the list rank below every listed state, ties broken
   alphabetically — an undefined `status:*` label must never out-vote a real
   one, and must still derive deterministically so the board-state check (#52)
   can report it.

3. The order encodes four bands, and the bands are the argument:
   **(1) the hold overlay** (`parked`) above everything, because a held ticket
   is held wherever it is and the stage label beside it is a resume target
   rather than a competing claim about the present; **(2) gates and waiting
   states** above the orchestrator's own claim labels, because when both are
   present the claim is the stale one — a handoff or human move already
   happened; **(3) session-running states**, ordered for totality rather than
   for a live case, since role-pinning means they should not coexist; **(4)
   `todo` last**, because it is what `_park` backfills and what the fail-review
   `hold` verdict writes beside the park label, making it the label most likely
   to appear as a *second* label.

4. `load_precedence` **raises** on an absent or malformed section rather than
   falling back to an implicit order. A silent fallback would restore the exact
   bug — a parked issue deriving `in progress` — with no signal.

5. Labels outside the `status:` namespace do not participate in derivation.
   This was already true; it is now pinned by a test, because it is the
   invariant part (b)'s `hold:parked` migration lands on.

**Observable behaviour changes in exactly one direction: a held ticket now
derives `parked` under every ordering of the writes.** No label is provisioned,
no live issue is relabeled, and `_park`'s label writes are byte-for-byte
unchanged. `honored_drags()` derives from `edges`, not `precedence`, and its
pinned result is unchanged.

## Rejected options, steelmanned

**Keep `sorted()[0]` and strip harder.** The cheapest option, and it has a real
case: the strip already exists, and every reader that matters (`_eligible`, the
review poll) gates on `PARK_LABEL` rather than on the derived state, so the
mis-derivation arguably harms nothing but the board. Rejected because the strip
is a *write* — it can fail, and it runs after the park write, so there is always
a window and a failure path where the wrong state is live. More decisively, this
option makes the alphabet a maintained interface: the fourth park-reachable
label already had to be checked against it by hand, and a fifth would too.
Correctness that depends on nobody choosing a label name starting with a letter
below `p` is not correctness.

**Rank by `active_states` instead of a hand-written list.** Attractive because
`active_states` is already per-project (`AgDR-039`) and the thing we actually
care about is "did the derived state clear the dispatch filter". Rejected
because it inverts the dependency: derivation would become project-dependent, so
the same two labels would mean different things on two boards, and the tracker's
"single source of truth for status:* -> state mapping" would stop being single.
It also cannot express the band that matters most — `parked` is inactive on
every project, and so is `human-review`, but they need different ranks relative
to each other.

**Do part (b) now — move the marker to `hold:parked` and delete the problem.**
The right end state, and this record does not argue against it. Rejected *for
this PR* because the migration writes labels on live issues and its risky step
(clear the old marker only after the new one is confirmed durable) has a window
where a poll sees an ordinary ready ticket and spends a session cap on work
somebody deliberately stopped. Part (a) writes no labels at all. Shipping the
derivation fix first also means the migration lands on a tree where `parked`
already outranks everything, so a half-migrated issue carrying both markers
still derives `parked`.

**Encode the precedence as a Python constant.** Simpler, no YAML load in the
derivation path, no failure mode when the file is missing. Rejected because
`transitions.yml` is the committed single source of truth for lifecycle rules
and `test_transitions.py` already pins "no transition-table literal in Python" —
a second copy of the state vocabulary in `tracker.py` is the drift that test
exists to prevent. The load is cached (`lru_cache`), so the cost is one read per
process.

## Blast radius

- **`tracker.normalize_status_state`** — every consumer of a derived state.
  Single-label derivation is unchanged (pinned by a test over every label
  `register-project.sh` provisions), so the change is confined to multi-label
  issues.
- **The multi-label pairs whose winner flips.** `parked` + `in-progress` and
  `parked` + `fail-review` now derive `parked` instead of an active state — the
  fix. `human-review` now outranks `fail-review` (it did not, alphabetically);
  that pair is reachable only through `handoff_label_rollback_failed`, and the
  new answer is the correct one. `triage` now outranks `in-progress`; the claim
  label is only ever written from `todo`, so the pair is unreachable today.
  `triage` also now outranks `todo`, which alphabetical order resolved the other
  way — the one flip among the pairs the verifier could in principle produce.
  It is unreachable through the documented path: every triage verdict is a
  SINGLE `gh issue edit` carrying both `--remove-label status:triage` and its
  `--add-label` (`WORKFLOW.base.md`'s verdict table pins this, marker included),
  so there is no window in which both labels are present. Were one to appear
  anyway, deriving `triage` re-dispatches the verifier — which takes the
  unchanged-body fast-path — rather than dispatching an implementer against a
  `status:todo` whose swap did not complete.
- **`board_sanity`'s multiple-live-labels finding** — the operator-facing text
  said the winner is "decided arbitrarily (sorted-first)", which is now false.
  Rewritten: derivation is deterministic, and the finding stands because the
  precedence exists to rank a hold above a stage, not to arbitrate between two
  stages.
- **`status_board`** — derives its option through `normalize_status_state`, so
  it inherits the ranking with no change. Latent under `AgDR-048` (the Action is
  not installed) but kept correct for the stated reopening gate.
- **`transitions.yml` readers** — a new section; `load_edges` and
  `load_requires_marker` are untouched.
- **Not touched:** what parks a ticket, the session cap, the unpark trigger,
  which stage label park leaves behind, `_park`'s strip/backfill behaviour, the
  fail-review episode mechanics, `honored_drags`.

## Weakest point

**The four bands are asserted from first principles, and only band 1 has
production evidence behind it.** `parked` outranking the claim labels is
motivated by an observed hazard with a filed ticket. The rest — gates above
claims, `todo` last, the internal order of the session-running states — is
reasoning about pairs that are unreachable or nearly so today. That is not
nothing (a rule that is total is worth more than a rule with holes), but it
means the list contains ranks nobody has tested against reality, and a rank
nobody has tested is a rank that can be wrong without anybody noticing.

**What would make this wrong:** a pair from band 2 or 3 turns up in production
and the operator's judgement about which label is stale disagrees with the list.
The concrete candidate is `human-review` + `in-progress` after a partial handoff
swap: this record says the gate wins because the handoff already happened, but
if the rollback path is later changed so that a *failed* handoff can leave the
gate label behind, the right answer flips and the ticket silently stops being
dispatched. Watch the `handoff_label_rollback_failed` log line — it is the only
producer of that pair, and if it starts firing regularly, the rank needs
re-deciding rather than the ordering being patched around.

**Second weakest point:** this record fixes derivation while leaving the hold
inside the `status:` namespace, so every *new* consumer still has to learn that
one `status:*` label is not a state. `DURABLE_STATUS_MARKERS` and
`live_status_labels` — the one-element carve-out and its parallel accessor —
survive untouched, and three notions of "the status labels" remain live in the
tree. Part (b) is what retires them. If part (b) is never done, this record will
read in a year as having made the wrong encoding *safe* rather than making it
*right*, which is exactly the shape of the deferral it is discharging.
