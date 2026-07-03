# AgDR-006: Triage rides the existing dispatch machinery as an active state

- **Status:** retroactively recorded at merge gate (2026-07-03) for the
  original PR #17 implementation (f8bfe3f), which shipped without a record.
- **Context:** Issue #11 wanted adversarial ticket verification before an
  issue becomes dispatchable. The verification step needs a session, budget
  caps, a workspace to investigate in, and a routing outcome.
- **Decision:** Model triage as a third active state (`status:triage` in
  `active_states`) whose dispatched session runs as a verifier — the role swap
  lives entirely in a Liquid branch of the workflow prompt. Reuses dispatch,
  session caps, budget, and parking unchanged.
- **Rejected:** (a) separate verifier daemon/process — duplicate scheduler for
  one prompt difference; (b) pre-dispatch orchestrator check — puts judgment
  in orchestrator code, violating the methodology-as-config principle that
  gates cost zero orchestrator code.
- **Blast radius:** low on code (prompt + labels), but the original claim "no
  new daemon or scheduler semantics" was disproved at review — the PASS
  handoff required the role-pin rule (AgDR-005). Consequence: prompt-level
  role swaps are only sound if the scheduler ends sessions on state change.
- **Weakest point:** roles are invisible to the scheduler — it dispatches
  states, not roles, so nothing structural stops a future prompt branch from
  implementing when it should verify. The role boundary is prose, enforced
  only by prompt review.
