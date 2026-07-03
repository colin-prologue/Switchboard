# AgDR-005: Role-pinned worker sessions (break on ANY state change)

- **Status:** ratified by Colin at PR #17 merge gate (2026-07-03; general
  rule chosen over triage-scoped alternatives, approved in session).
- **Context:** Codex review P2 on PR #17: a triage PASS relabels
  `status:triage → status:todo`, both active states, so core §16.5's loop
  ("break when state leaves active_states") kept the verifier session alive —
  continuation prompts to a stale role until max_turns/budget, holding a slot
  and blocking implementer dispatch.
- **Decision:** The worker breaks its turn loop whenever the refreshed state
  differs from dispatch-time state, active → active included. Re-dispatch
  starts a fresh session in the new role. Owned override of core §16.5,
  recorded in spec/SPEC.md §4; folded into PR #17 rather than a follow-up
  issue because METHODOLOGY.md's "no new scheduler semantics" claim would
  otherwise ship knowingly false.
- **Rejected:** (a) triage-scoped break (`if dispatch_state == "triage"`) —
  hardcodes a workflow label into the scheduler core; (b) config-driven
  handoff-state list — more surface for the same behavior, no second consumer
  yet; (c) PASS routes to an inactive state — kills the auto-promotion flow;
  (d) accept bounded waste — makes max_turns the routine exit for every
  passing triage, polluting the caps-as-diagnostic-checkpoints signal.
- **Blast radius:** every future workflow state design. An agent can no longer
  relabel its own issue mid-session and keep working — a state transition is
  always a session handoff.
- **Weakest point:** forecloses mid-session progress self-marking (e.g.
  implementer setting `status:in-progress`, today aspirational with no call
  sites). Each such transition would end the session and spend one of
  `max_sessions_per_issue: 3`. If adopted, raise the cap; don't weaken the
  break rule.
