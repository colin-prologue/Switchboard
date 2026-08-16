# AgDR-033 — Session budgets are accounted per role, not per issue

- **Status:** proposed (ratify at the merge gate)
- **Issue:** #35
- **Date:** 2026-08-08
- **Supersedes (in part):** AgDR-002's per-issue counter shape (the cap value,
  the durable park label, and "caps as diagnostic checkpoints" are unchanged)

## Context

`agent.max_sessions_per_issue` (default 3) was accounted by `issue.id` alone —
role-blind and process-lifetime. `status:triage` is an **active** state, so a
verifier session is dispatched and counted exactly like an implementer session
(`scheduler.py` @ 5aad618: init `:178`, cap gate `:544-547`, increment `:630`,
refund `:866-870`; resets only on unpark `:499`). Combined with AgDR-005
role-pinning — the verifier's own PASS relabel ends its session at the next turn
boundary — a ticket that needed several adversarial verify passes arrived at
`status:todo` with the budget already spent and parked on the first implementer
dispatch. Live instances: #30 (2026-07-05), #15 and #57 (both 2026-07-12,
same-day parks mid-triage).

Verification and implementation are different jobs on different contracts. One
counter spanning both means scrutiny is charged to the work it is supposed to
protect: the more adversarial the triage, the less implementation budget
survives it. That is backwards.

## Decision

Key the counter by **(issue id, session role)**. The role is derived from the
dispatch-time state — `status:triage` → `verify`, every other active state →
`implement` (`session_role()` in `scheduler.py`) — and frozen on the
`RunningEntry` for the life of the session. `max_sessions_per_issue` is applied
**per role**, so each role gets the full budget; the cap *value* is unchanged
(issue #35 non-goal) and no new config knob is added.

Consequences at every reader of `sessions_per_issue`:

| Reader | Change |
|---|---|
| init | value type `dict[tuple[str, str], int]` |
| unpark reset | `_reset_issue_sessions()` — drops **both** role counters |
| cap gate | keyed by `(id, role)`; park reason names the exhausted budget |
| increment | keyed by `(id, role)`; adds a `session_role` log field beside `session_number` |
| failure refund | takes `RunningEntry.session_key` — refunds the budget the session was *drawn from*, not the one the issue's current state implies |
| read surface | new `sessions_for_issue(id) -> {role: spent}`; a bare-id `get()` is now a silent miss, so every reader (tests included) goes through it or builds the tuple |

The park **comment body** distinguishes the budget — `verify budget exhausted
(3/3 verify sessions)` vs `implement budget exhausted (3/3 implement
sessions)`. The **label set is unchanged**: `status:parked` is the sole park
label, and an unprovisioned second label would halt dispatch process-wide
(`scheduler.py:393-399`).

## Rejected option (steelmanned): reset the counter on the triage→todo transition

The strongest version: keep the counter keyed by `issue.id`, record the role the
last session ran under, and zero the counter when the role changes. It is a
smaller diff — the dict type is untouched, so none of the ~20 test assertions
that index by bare issue id need to move, and the #44 unlisted-reader risk drops
to near zero. It also matches the intuition that a role change is a genuinely
fresh engagement.

It loses on three counts:

1. **Ping-pong is unbounded.** `todo → drafting` (NEEDS WORK) `→ triage → todo`
   is a legal, agent-drivable cycle, and every lap through it resets the
   counter. The cap stops being a spend ceiling and becomes a per-role-hop
   allowance — the OBS-022 self-unpark failure class in a new costume.
   Composite keys have no such loop: the verify counter that a ticket
   accumulated is still there when it comes back around.
2. **It destroys the evidence.** The park comment and the `session_number` log
   field are the forensic trail (#30's park was diagnosed from exactly these).
   A reset erases how much verification a ticket consumed before it was
   implementable — the number that tells you a ticket was under-specified.
3. **It stores the role anyway.** To detect "the role changed" you must remember
   the previous role, so you pay the same bookkeeping and get a lossy counter
   for it. Composite keys make that state explicit and readable instead.

The test churn that argues for reset is one-time and mechanical; the counter
shape is permanent.

## Blast radius

`scheduler.py` only (state machine); no tracker/label/config surface changes, so
nothing downstream of the orchestrator observes a new vocabulary. Behaviour
changes for exactly one class of issue: one dispatched through `status:triage`
now reaches `status:todo` with a full implementer budget. Worst realistic spend
increase is 2× `max_sessions_per_issue` for a single ticket (a full verify budget
*plus* a full implementer budget), which is the intended semantics, not a
regression — it was previously "free" only because the ticket parked instead of
being implemented. `spec/SPEC.md` §4 and `methodology/METHODOLOGY.md` (triage
section) are updated to state the split.

## Weakest point

The role map is a hard-coded state set (`VERIFY_STATES = {"triage"}`) rather than
config, so a project that renames or adds a verification-flavoured active state
silently gets implementer accounting for it. That is deliberate — per-project cap
config is an explicit non-goal of #35 — but it is the seam that will need
reopening if a second verifier-like state ever lands. The second-weakest point:
the role is derived from *dispatch-time* state, so a session dispatched at
`triage` that a human relabels to `todo` mid-turn is still charged to the verify
budget. Freezing the key is what keeps the refund path honest, and the role pin
ends that session at the next turn boundary anyway, so the mischarge is bounded
to one session.

---

## Its own weakest point fired (2026-08-15)

This record named the seam:

> The role map is a hard-coded state set (`VERIFY_STATES = {"triage"}`) rather
> than config, so a project that renames or adds a verification-flavoured active
> state silently gets implementer accounting for it. […] it is the seam that
> will need reopening if a second verifier-like state ever lands.

One landed: the `prototype` stance's `status:review` (`AgDR-039`). The
prediction was exact — the QA session ran as `implement`, shared the
implementer budget, and exhausted it early, which is the failure this record
exists to prevent.

**The first fix added `"review"` to the literal.** That closes the seam again
with two members instead of one, and leaves the next stance to rediscover the
same silent failure. Recorded because it is the more interesting half: a
prediction firing does not guarantee the response addresses what was predicted.

**The seam is now derived rather than declared.** A dispatched handoff target is
by definition where review happens, so `session_role` asks
`TrackerConfig.handoff_state()` and `agent_owns_gate_c()` — the same two fields
`AgDR-043` reads, asked a different question. A stance naming its review state
anything at all is accounted correctly with no edit to `scheduler.py`.

`triage` remains a literal, and irreducibly so: it is an ordinary active state
that happens to run an adversarial verifier, and nothing in config marks it as
such. One declared floor plus derivation for the stance-driven case is the
honest shape — not zero hard-coding.