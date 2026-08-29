# AgDR-049 — Both actors on the re-entry edge reset the implement budget, under one shared durable round bound

- **Status:** proposed (ratify or overturn at the #178 merge gate)
- **Issue:** #178 — A human changes-requested relabel does not reset session
  counters, so it can park instead of re-dispatching
- **Date:** 2026-08-29
- **Supersedes / amends:** amends AgDR-037's `ROUND_CAP` from *the sub-poll's
  budget* to *the edge's budget*, shared by both of its actors. Narrows
  AgDR-047's fail-review routing by removing one way its once-per-issue episode
  could be spent on a non-failure. Leans on AgDR-048 for the trust model.

## Context

`workflow/transitions.yml` sanctions **two actors** on `human-review → todo`:
the orchestrator's review-response sub-poll (#43 / AgDR-037), and a human
requesting changes. The two write the same label and mean the same thing —
"another implementation round, please."

Only one of them reset the session counters. The sub-poll calls
`_reset_issue_sessions` before its relabel; the human path reset nothing,
because no code observed a manual label edit at all.

So a ticket whose implement budget was already spent, handed to a human, then
relabelled by that human with review comments, re-entered dispatch and hit the
cap check. The outcome forked on AgDR-047's durable `gate:fail-reviewed` marker,
and **neither branch was a re-dispatch**:

- **Marker absent** — the ordinary case, since most tickets have never failed.
  The cap-hit routed to `_route_to_fail_review`. A diagnostic verifier was
  dispatched against a failure that never happened, it did not address the
  review comments the human wrote, and because the marker is once-per-issue and
  durable, **the revision request permanently consumed the issue's only
  fail-review episode**. This is the worse branch: it silently spends a scarce
  diagnostic resource.
- **Marker present** — the ticket parked with `implement budget exhausted`. The
  operator asked for a revision and was told the system gave up.

One edge, one resulting label, three budget outcomes depending on who took it
and what the issue had already been through — and only the orchestrator's
outcome was documented.

The unpark path is not the answer. It does reset counters, so the operator *can*
recover from the park — but that is a second manual step discovered only by
hitting the park, it inverts the meaning of the labels, and it does nothing at
all for the marker-absent branch, which never parks.

## Decision

### 1. Observe the human relabel and grant the same reset (Option A)

The orchestrator records the status state of every open issue each tick, and its
own review-response relabel writes its attribution into that same map. A
`human review → todo` change the orchestrator did not record itself is, by
construction, somebody else's write — so it grants the fresh implement budget
the sub-poll's path already grants.

The alternative — leaving counters alone and instead teaching the cap-hit branch
that the issue arrived from `human-review` — is smaller, and it was seriously
considered (see below). It was rejected because it fixes the symptom on one
branch and leaves the two actors permanently disagreeing about what the edge
means.

**The grant runs before dispatch within the same tick.** Run after, and the
operator's revision request still routes to fail-review or parks once, one tick
before the fix takes effect. It reads the *unfiltered* open-issue list, not the
candidate list, because `human review` is a gate state the candidate filter
removes — and that is the state the transition starts from.

### 2. ONE round budget, shared by both actors

The human path draws on AgDR-037's existing `ROUND_CAP = 2`, not a count of its
own. A per-actor count would let the two alternate for 2× the rounds the cap was
chosen to permit, which is the cap not binding.

The bound rides the same durable per-PR marker comment the sub-poll writes, so
it survives a restart. At the cap the orchestrator stops granting and comments
instead — a **second** one-shot comment with its own marker, deliberately not a
reuse of the sub-poll's: that one offers "move the issue back to `status:todo`"
as the recovery, which is precisely the action a human at this cap has just
taken. A shared guard would let whichever fired first suppress the other's
explanation on the same PR, and the operator would be told nothing.

### 3. Three refusals, each for its own reason

- **Nothing spent** → no grant. The budget is already fresh, so a grant would
  buy nothing and consume one of two scarce rounds. This is also the common case
  — an ordinary revision request on a ticket that never hit its cap — so it must
  cost zero API calls, and does.
- **No App identity** → no grant, logged once. `latest_round` trusts only
  markers authored by the normalized `$SB_APP_BOT_LOGIN`; unset, the count reads
  0 forever and the bound would not bind at all. An unbounded reset is worse
  than the bug being fixed. Same posture the sub-poll takes on the same env var.
- **No bindable PR** → no grant. The bound lives in a comment *on the PR*. With
  no PR there is nowhere to record that a round was spent, and an unrecorded
  grant is exactly the refill-by-relabelling the cap exists to prevent.

The marker is written **before** the reset, the same ordering argument AgDR-047
makes: a crash between them burns a round harmlessly, while the reverse hands
out a budget the cap can never account for.

### 4. The detection map is process-lifetime, and that is not #15's weakness

It has exactly the lifetime of `sessions_per_issue`, which is the state it
exists to correct. A restart empties both — so the ticket the map has forgotten
is also the ticket whose budget is already fresh, and it dispatches, which is
the outcome the grant exists to produce. The durable half of this feature is the
round marker on the PR. **This ticket therefore does not wait on #15**, and #15
landing would change the mechanism without changing any acceptance criterion.

## Rejected alternatives (steelmanned)

- **Option B — bound the cap-hit routing instead of resetting.** Materially
  smaller blast radius: the routing branch plus a comment body, mutating nothing
  the nine readers of `sessions_per_issue` observe. Genuinely the safer change,
  and the one to prefer if the reset turns out to have a consumer nobody
  enumerated. Rejected because it does not make the two actors agree — it
  teaches one code path to special-case an arrival state, and the next reader of
  `transitions.yml` still finds an edge whose meaning depends on who took it.
  Note too that the appealing "just improve the park message" reading of Option
  B covers only the marker-present branch; in the marker-absent branch there is
  no park message to improve, only a mis-routed episode.
- **A separate round budget for the human path.** Reads as fairer — why should a
  reviewer's request be rationed by the bot's activity? Rejected: two budgets on
  one edge is 2× the allowance, reachable by alternating actors, and the cap's
  purpose is a bound on total rounds rather than a per-actor allowance.
- **Reset unconditionally, with no bound at all.** The operator is trusted
  (AgDR-048), so policing them is arguably ceremony. Rejected because the bound
  is not primarily about trust — see the weakest point below.
- **Forbid the human path and make the operator use unpark.** Would resolve the
  disagreement by deleting one actor, and it is the correct fix *if*
  `transitions.yml`'s two-actor annotation were aspirational. It is not: the
  annotation is explicit and deliberate. Rejected on that basis.
- **Detect the relabel from GitHub's issue timeline / event API instead of a
  local map.** Authoritative — it names the actual actor rather than inferring
  one from a bookkeeping gap. Rejected as a new API surface and a per-tick cost
  to answer a question local state already answers correctly, given that the
  orchestrator is the only other writer of this edge.

## Blast radius

- **`sessions_per_issue` gains a tenth writer**, and every existing reader
  observes it: the dispatch cap check, the fail-review routing branch, the park
  path, the episode start and its idempotent restore, the idempotent dispatch
  guard, unpark, the sub-poll, and `sessions_for_issue`.
- **The fail-review branch is the one that changes behaviour most quietly.** A
  reset counter means the cap-hit never fires and `_route_to_fail_review` is
  never reached on this path. That is the intent — the episode stops being spent
  on non-failures — but it does mean an issue that would previously have been
  diagnosed after a human revision request now simply gets another
  implementation round. Covered by a test in `test_fail_review.py` that fails on
  the marker being written, not on an internal probe.
- **`has_cap_comment` gains a `marker` parameter**, defaulted so every existing
  caller is unchanged.
- **The tick gains an ordering dependency between two of its steps.** The
  observer rebuilds its whole map from the open-issue list *before* dispatch;
  the sub-poll writes its self-attribution at the *end* of the tick. That order
  is what makes the attribution survive — reversed, the rebuild would clobber
  it and the next tick would read the sub-poll's own relabel as a human's,
  burning a second round on a budget just granted. Both call sites carry a
  comment saying so, and the sub-poll's attribution is covered by a test, but
  nothing mechanically pins the two steps' relative position in `_tick`.
- **One new PR comment shape** the operator may see, on a PR at the round cap.
- **Every tick now walks the unfiltered open-issue list once more.** Pure local
  bookkeeping; zero API calls until a transition is actually seen, which is at
  most once per relabel.
- **`METHODOLOGY.md`'s writer table and `transitions.yml`'s edge note both
  change their claim** about who resets. They previously documented the
  asymmetry as fact; leaving them would strand the next reader with a belief the
  code no longer holds.

## Weakest point

**The bound is justified as anti-evasion, and under AgDR-048 there is no
adversary to evade.** This is a single-operator system; the operator can already
unpark, edit the cap, or restart the process to refill any budget. Framing the
cap as protection against "an operator refilling an issue's allowance by
relabelling" describes a threat model this project explicitly does not have.

The honest justification is narrower, and it is the one to hold this record to:
the cap bounds an **unattended loop**, and it makes budget grants *legible* by
recording each one on the PR. A relabel is a deliberate human act, so the loop
risk is low — which means if the two-round bound ever becomes an irritation in
practice, the right response is to raise or remove it for this actor, not to
defend it on evasion grounds. **What would make this wrong:** the operator
hitting the relabel cap during ordinary review work even once. That is the
signal that the bound was priced for a risk that does not exist here.

Second: detection is *inference*, not observation. A third writer of this edge —
a future script, another integration, a `gh` alias in CI — would be attributed to
"a human" and would receive a budget grant, silently. Nothing checks that the
unaccounted-for write came from a person. The mitigation today is that the
orchestrator is the only other writer, which AgDR-048's one-process-per-repo
constraint holds up; the day that stops being true, this inference needs the
timeline API the rejected-alternatives section declines to build.

Third, narrowest: a grant is recorded on the PR *bound to the issue at grant
time*. An issue whose PR is closed and replaced mid-review starts a fresh
two-round budget on the new PR. That is defensible — a new PR is a new review —
but it is a refill path this record should not pretend is closed.
