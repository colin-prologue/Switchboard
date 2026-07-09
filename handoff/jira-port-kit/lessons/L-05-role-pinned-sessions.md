# L-05 — Role-pinned sessions: any state change ends the session

**Context.** Switchboard's triage verifier, on PASS, promoted the ticket to
the dispatchable state. Under the upstream core's rules the session stayed
alive across that transition — meaning the *verifier* would keep taking turns
as an *implementer*, with all its verification context and biases intact.

**Decision.** When a running session's ticket changes workflow state — even
active→active — the worker breaks at the next turn boundary and re-dispatch
starts a fresh session in the new role. Rejected alternatives: hardcoding the
triage transition into the scheduler (leaks workflow into core), a
config-driven handoff list (surface area, no second consumer), routing PASS
through a human gate (kills auto-promotion).

**Lesson.** Roles are workflow-level; the scheduler only knows states. When
one session can cross a role boundary, you get role confusion — an agent
grading its own homework or implementing with a critic's partial context.
Fresh session per role is the cheap structural fix.

**Cost (accepted knowingly).** Agents cannot self-mark progress with state
transitions mid-work — a transition would end their own session. Keep the
workflow coarse: transitions mark role handoffs, not progress. If a
transition-heavy Jira workflow is imposed on you (see board-control
constraint), this rule and that workflow will fight; resolve it as an ADR,
and if you must allow a mid-work transition, raise the session cap rather
than weakening the break rule.

**Portable.** Entirely, and it composes with I-14: with N daemons, a state
change made by *someone else's* daemon (or a human) is detected the same way
and cancels the now-stale session — one mechanism covers both role handoff
and multi-writer reconciliation.
