# Target Context — Decided Facts About the L&W Environment

Ground truth as of 2026-07-06. If any of this changes, Colin revises this file
(I-11: stale executable context gets invalidated here, not discovered later).

## Environment

- **Tracker:** self-hosted **Jira Data Center** (not Cloud). An existing
  **jira-mcp** is already in use for other Claude interactions and is the
  expected pathway for agent↔Jira operations. Its exact capability surface is
  unverified — see CONSTRAINTS-TO-ESTABLISH.md.
- **Code:** **GitHub** (Enterprise or .com — confirm which). Branch
  protection and required PR approvals are the expected enforcement surface
  for the merge gate (I-6).
- **Execution:** Claude sessions on each engineer's own machine.

## Decided operating model (v1)

- **Hybrid, both halves in v1.** Engineers practice the ticket protocol
  interactively (well-documented tickets, shared pool) *and* run per-engineer
  daemons. The interactive practice trains the team to trust the daemon pool;
  the daemon/orchestrator is not deferred to a later phase.
- **N daemons, one board.** Any number of engineers run daemons on their own
  systems against the same shared Jira board. Multi-writer safety (I-13/14/15)
  is a v1 requirement, not an enhancement.

## Decided identity model

- **Operator identity.** Agents act under the identity of the engineer whose
  machine dispatched them (their Jira auth via jira-mcp, their GitHub
  credentials). Provenance = accountability: you can always tell whose daemon
  did it.
- **Gate C is peer review.** Because the dispatching engineer is the effective
  author, PR approval must come from a *different* engineer. The team setting
  provides this naturally; enforce with required approvals, and mark
  agent-authored work visibly (branch naming, PR template, or commit trailer)
  so reviewers know what they're reviewing.

## Deliberately NOT ported from Switchboard (incidental mechanics)

- `status:*` **labels as state** — a workaround for GitHub having no
  first-class workflow states. Jira has real statuses; how gate states map to
  them (native statuses vs. label/field overlay) depends on board control —
  see CONSTRAINTS-TO-ESTABLISH.md.
- **GitHub App/bot identity** — superseded by the operator-identity decision.
- **Python/asyncio and the vendored Symphony spec** — implementation choices,
  not principles. Choose your own substrate; L-01 explains what the vendoring
  decision was actually about.
- **The exact gate taxonomy** (drafting/triage/todo/in-progress/plan-review/
  human-review/blocked/parked) — the *pattern* (gates as non-dispatchable
  states, an adversarial triage gate, a parked state) is constitutional; the
  specific set should be redesigned for a team + Jira context.
