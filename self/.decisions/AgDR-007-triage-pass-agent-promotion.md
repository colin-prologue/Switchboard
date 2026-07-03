# AgDR-007: Triage PASS lets an agent promote a ticket to dispatchable

- **Status:** ratified by Colin at PR #17 merge gate (2026-07-03).
  Retroactively recorded for the original implementation (f8bfe3f); was
  flagged most contestable call of this pass.
- **Context:** Gate A (METHODOLOGY.md) reserves the `status:drafting →
  status:todo` promotion for a human: "the agent never sees an unapproved
  ticket." Triage PASS gives an *agent* a promotion path to `status:todo`
  (from `status:triage`), i.e. an agent verdict now makes a ticket
  dispatchable with no human in between.
- **Decision:** Accept agent-performed promotion on the triage path. The
  human control moves one step earlier: a human (or SPLIT parent) chooses to
  file at `status:triage` at all, and skip-triage guidance keeps trivial
  tickets on the human Gate A path. NEEDS WORK / SPLIT verdicts route back to
  `status:drafting`, i.e. only the strictest verdict promotes.
- **Rejected:** (a) PASS routes to a `status:triage-passed` gate state a human
  promotes — reintroduces the human tollbooth triage exists to relieve and
  doubles the states; (b) verifier comments verdict but a human relabels —
  same, with worse ergonomics.
- **Blast radius:** gate semantics. Implementation sessions (real spend, real
  code) can now be triggered by an agent's judgment of another author's
  ticket. Bounded by: budget/session caps, Gate C (human merge) still
  binding, and triage filing being a human act.
- **Weakest point:** a lenient verifier converts a bad ticket into burned
  implementation sessions with no human checkpoint until PR review. The
  calibration signal (bounded tickets round-trip, unbounded ones burn a
  session and park) is trailing, not leading.
