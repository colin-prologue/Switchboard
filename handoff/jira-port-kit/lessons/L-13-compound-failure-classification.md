# L-13 — Classify every root of a compound failure; pick recovery per root

**Incident.** A parked Switchboard ticket turned out to have *two*
simultaneous root causes: a permission wall (the agent was denied a
capability it needed) AND under-scoped test coverage in the ticket itself.
Clearing the permission wall alone — the visible bottleneck — would have
re-dispatched a ticket still doomed to burn its remaining sessions.

**Lesson.** When a failed/parked ticket presents multiple root causes,
classify and handle each separately; single-point fixes to the observable
bottleneck mask the structural problem behind it. The recovery policy
differs by root class:

- **Capability wall** (permission, missing dependency, quota): fix the wall,
  re-dispatch *with full prior context* — the work done so far is valid.
- **Unproductive path** (thrash, rabbit-holing): fresh dispatch with a
  facts-only brief that deliberately *excludes* the prior session's
  conclusions — the context is the contamination.
- **Scope overflow** (ticket too big): split the ticket; retrying at the
  same scope re-buys the same failure.

**Where it lives in the design.** This is the human playbook for parked
tickets (L-03): parking preserves the diagnostic trail precisely so this
classification can happen. Consider making the classification a field on the
unpark act — with N engineers unparking each other's tickets, a shared
taxonomy beats N private heuristics.

**Portable.** Entirely; applies to any bounded-retry system (orchestrators,
CI, incident response).
