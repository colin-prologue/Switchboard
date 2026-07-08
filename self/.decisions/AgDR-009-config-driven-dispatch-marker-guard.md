# AgDR-009: Config-driven dispatch-marker guard — a bounded AgDR-006 exception

- **Status:** proposed by the issue #29 implementing session (2026-07-07);
  awaiting ratification at the PR merge gate.
- **Context:** The `status:*` labels imply a lifecycle, but nothing enforced
  which transitions are legal. Tickets reached `status:todo` without passing
  `status:triage` (#10, #20 burned 6 sessions on un-sized work). AgDR-006
  established the methodology-as-config principle — "every gate costs zero
  orchestrator code" (`methodology/METHODOLOGY.md:6`) — by rejecting a
  pre-dispatch orchestrator check in favour of prompt/label-driven gates. But
  the one rule that must hold *before* an agent is spawned ("do not dispatch a
  `todo` that never passed triage") cannot live in the prompt: the prompt only
  runs once the session already exists. It needs a check in the dispatch path.
- **Decision:** Add exactly one pre-dispatch check to the orchestrator, kept
  config-driven to honour AgDR-006's intent rather than its letter. The shared
  `workflow/transitions.yml` carries a per-state `requires_marker` map; the guard
  loads it from ONE committed path constant (`transitions.TRANSITIONS_PATH`) and
  refuses to claim an issue whose current state requires a marker it lacks —
  today only `todo` requires `gate:triage-passed`. Refusal is inert: no claim,
  no label writes, one repost-guarded comment naming the missing marker, issue
  left untouched. No transition *edges* live in Python — the orchestrator polls
  current state and cannot observe a `from`, so it reads only `requires_marker`;
  the `edges` section is consumed by #52's Action, which sees both endpoints in
  its event payload. The provenance marker itself is produced by the triage
  verifier's PASS command (prompt change), not by orchestrator code.
- **Rejected (steelmanned):**
  - *(a) Keep it purely prompt/label-driven (strict AgDR-006).* Impossible for
    a pre-spawn gate: the prompt is the thing being gated. The only way to keep
    judgment out of the orchestrator entirely is to accept dispatch of un-triaged
    todos, which is the failure this ticket exists to close.
  - *(b) Encode full transition edges (with `from`) in the orchestrator.* Lets
    the guard reason about legality generally, but forces cross-restart edge
    reconstruction from a stateless poll — the exact durability trap AgDR-008
    escaped. The orchestrator has no memory of the prior state after a restart.
  - *(c) A `status:triage-passed` gate *state* (human tollbooth).* Rejected by
    AgDR-007 already: it inserts a human step and a new status column. The marker
    is a non-status provenance label applied automatically by the same actor in
    the same command — no new column, no human step, no tracker-derivation
    change (`tracker.py` keys state on `status:*` only).
  - *(d) Hard-code the `{todo: [gate:triage-passed]}` map in Python.* Fast, but
    drifts from #52's copy of the same rules and reintroduces a table literal in
    code. The single-committed-file + path-constant AC exists to prevent this;
    a pytest greps the orchestrator source to keep it honest.
- **Blast radius:** low and bounded. One new pre-dispatch branch in
  `_dispatch` (covers both poll and retry paths); one new stdlib+yaml loader
  module; a behaviour-preserving extraction of `normalize_status_state` in
  `tracker.py` (no derivation change — non-goal honoured). The guard only ever
  *withholds* dispatch and posts one comment; it never mutates labels or state.
  If `transitions.yml` is missing/malformed the orchestrator fails loudly at
  construction (like a broken workflow), not silently.
- **Weakest point:** the guard trusts the marker as proof of triage, but the
  marker is just a label — anyone (today, everyone is `colin-prologue`) can hand-
  apply `gate:triage-passed` to a `todo` and bypass triage entirely. v1
  validates state/marker presence only; actor-conditional enforcement ("only the
  triage verifier may apply this marker") needs #10's provenance and is
  explicitly deferred. Until then the marker is a guardrail against *accidental*
  skips (the #10/#20 failure mode), not a security boundary against deliberate
  ones. Secondary: this is a genuine widening of the orchestrator's
  responsibility surface — the next "just one more check" must be measured
  against AgDR-006 again, not waved through by citing this record.
