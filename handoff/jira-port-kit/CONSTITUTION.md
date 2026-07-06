# Constitution — Non-Negotiable Invariants

Two tiers. **Tier 1** invariants were proven by operating Switchboard (each
cites the lesson file carrying the incident). **Tier 2** invariants are
derived extrapolations for the multi-operator setting — required, but honest
about being untested: no prior system of ours ran N concurrent daemons against
one board. Where Tier 2 forces a design choice, record it as an ADR.

Every invariant names its expected **enforcement mechanism**. A constraint
enforced only by prose in a doc or prompt does not count as enforced (I-1).

## Tier 1 — Proven

**I-1. Coordination constraints are machine-enforced, never prose-only.**
Merge ordering, claim exclusivity, gate rules — encode them in CI gates,
branch protection, workflow-transition rules, or scheduler logic. Prose binds
only readers; both agents and humans skip it under timing pressure.
Enforcement: if you find yourself writing "remember to X before Y" in a doc,
stop and build the check. (lessons/L-09)

**I-2. Isolation is provisioned before concurrency is granted, never
reactively.** Every agent session gets its own clean workspace (fresh clone or
worktree, branch per ticket) by construction. Safety that depends on
accidental non-overlap of writes is not safety. The same discipline applies to
the board: ticket state is a shared resource; claiming it must be exclusive
before any work starts. (lessons/L-10)

**I-3. Gates are workflow states the scheduler never dispatches from.**
Human checkpoints (draft review, plan review, PR review, parked) are modeled
as ticket states outside the scheduler's active set. This makes gates cost
zero orchestrator code and makes methodology changes config changes.
Enforcement: the dispatch query itself — ineligible states are structurally
invisible to daemons. (lessons/L-02)

**I-4. Operational markers that inform future behavior live in the tracker,
not in daemon memory.** Parking, claim state, anything a restart must not
forget — the board is the source of truth. In-memory state dies with the
process; with N daemons it was never authoritative to begin with.
(lessons/L-04)

**I-5. Caps are diagnostic checkpoints, not kill switches.** When a ticket
exhausts its session/spend cap, the system parks it: one visible notification,
a durable parked marker, claim released. Unparking is a deliberate human act
(explicit marker removal), never a side effect of ticket activity — a
notification that bumps ticket metadata must not be able to wake the ticket
that produced it. (lessons/L-03)

**I-6. Agents never merge. Approval comes from a human other than the
effective author.** With operator-identity agents, the dispatching engineer is
the effective author — so a *different* engineer approves. Enforcement: branch
protection + required approvals on GitHub, not convention. (lessons/L-07)

**I-7. Sessions are role-pinned: any change to the ticket's workflow state
ends the running session at the next boundary.** Re-dispatch starts a fresh
session in the new role. A verifier must never continue as an implementer
inside one context. Corollary: keep workflow transitions coarse — mid-work
transitions kill sessions. (lessons/L-05)

**I-8. Verification before autonomy — for tickets and for the system
itself.** Tickets pass an adversarial triage gate before an implementation
session may burn spend. New system capabilities that mutate shared state ship
a read-only/proposal phase first and earn autonomy with measured
false-positive rates. (lessons/L-06, L-15)

**I-9. Finalize, then publish — atomically.** Never mutate an artifact after
it is visible in a namespace another process consumes (write the file, then
move it into place; complete the ticket fields, then transition it). This is
the root pattern behind an entire class of concurrency defects.
(lessons/L-11)

**I-10. Goal conditions are adversarial specifications.** Any machine-checked
success condition will be satisfied by the cheapest path available. Before
running an autonomous loop against a check, enumerate the cheapest bypass and
price it above the real work. (lessons/L-12)

**I-11. Executable artifacts are invalidated on pivots, at the executor's
altitude.** Kickoff tickets, goal-prompts, runbooks execute faithfully even
when stale. When a spec or model pivots, re-point or invalidate every
downstream executable artifact, and put the staleness warning where the
executor reads (the ticket), not in a branch-local doc. (lessons/L-14)

**I-12. One authority per state store.** Within a daemon, scheduling state is
mutated from exactly one place (one loop/thread); workers report outcomes,
never mutate. Across daemons, the board is the shared store and I-13 governs.

## Tier 2 — Derived for multi-operator (untested; ADR each choice)

**I-13. Claiming a ticket is an atomic, verified compare-and-swap on the
board.** A daemon claims by transitioning ticket state (and/or assignee) and
then *reads back* to confirm it won — Jira DC gives you conditional-update
semantics only through transitions, so verify, don't assume. Two daemons
racing must resolve to exactly one owner with no human adjudication.

**I-14. No daemon ever assumes it is the only writer.** Every dispatch
decision re-reads current board state; a running session whose ticket state
diverges from dispatch-time state is cancelled and reconciled. All daemon
writes are idempotent or guarded, because retries and races will happen.

**I-15. Claims must be recoverable when their owner disappears.** Engineer
laptops sleep, lose network, go home. A claim without a liveness mechanism
(lease/TTL/heartbeat — design choice) becomes a permanently stuck ticket.
Recovery must be safe against the owner waking back up mid-recovery (I-9,
I-14 apply).
