# L-04 — Operational state lives in the tracker, not in daemon memory

**Context.** Switchboard first held parking in an in-memory set. A process
restart re-granted the full session cap to already-parked tickets — worst
case, cap × per-session budget re-spent per parked ticket, silently. The park
marker moved to a tracker label; the in-memory set was demoted to
bookkeeping.

**Lesson.** Any state that must inform *future* behavior across a restart —
parked, claimed, attempts-used — belongs on the board. The tracker is the
system's only durable, shared memory. Write ordering matters: the durable
write happens first, the in-memory mirror second, and failure of the durable
write must fail closed (see L-03's error-path incident).

**Accepted residual (know the trade).** Switchboard kept the per-ticket
session *counter* in memory: a restart mid-cap (2 of 3 sessions spent, not
yet parked) re-grants a fresh cap. Bounded cost, accepted for simplicity.
For N daemons this trade changes: a counter in one daemon's memory was never
visible to the others, so attempts-used likely needs to live on the ticket
too — or be explicitly scoped per-daemon as a design decision.

**Portable.** The principle and the write-ordering discipline, entirely.
Jira gives you better primitives than GitHub labels (custom fields, statuses)
— but the same rule: one durable source of truth, board-side.
