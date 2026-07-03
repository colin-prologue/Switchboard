# AgDR-006: Triage rides the existing dispatch machinery as an active state

- **Status:** ratified by Colin at PR #17 merge gate (2026-07-03).
  Retroactively recorded for the original implementation (f8bfe3f), which
  shipped without a record; rejected option (c) reviewed explicitly.
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
  gates cost zero orchestrator code; (c) context-free subagents / a dedicated
  verifier pool (raised at merge-gate review, 2026-07-03) — the dispatched
  session is already a fresh clone with zero author context, so extra workers
  add dispatch cost without marginal independence (PHI-038). The residual
  bias is same-model/same-rubric correlation, which subagents don't fix; the
  effective future knob is a different-model verifier (per-state execution
  command), deferred until calibration shows lenient-verifier failures.
- **Blast radius:** low on code (prompt + labels), but the original claim "no
  new daemon or scheduler semantics" was disproved at review — the PASS
  handoff required the role-pin rule (AgDR-005). Consequence: prompt-level
  role swaps are only sound if the scheduler ends sessions on state change.
- **Weakest point:** roles are invisible to the scheduler — it dispatches
  states, not roles, so nothing structural stops a future prompt branch from
  implementing when it should verify. The role boundary is prose, enforced
  only by prompt review.
