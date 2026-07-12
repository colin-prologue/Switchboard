# AgDR-012: graph-review ships proposals-only (Phase 1) before any mutation

> Renumbered from AgDR-009 (2026-07-12): a parallel worker session took the
> same number for the App-identity decision, which merged first and keeps it.

- **Status:** accepted (2026-07-05). Authored in-session with issue #37 (the
  design branch `docs/v0.2-graph-review-design` was not reachable from the
  worker; the issue's own fallback authorizes implementing the intent in this
  PR). Ratify or overturn at the merge gate together with the binding intent
  `self/.switchboard/intents/graph-review.md`.
- **Context:** graph-review wants to reconcile the board's *latent* structure
  (prose dependencies, duplicate tickets, missing milestones, assumptions a
  merged PR invalidated) with its *enforced* structure (native `blockedBy`
  edges, milestones, labels). The tempting shape is one pass that both detects
  **and** fixes: add the edge, set the milestone, merge the tickets. That pass
  mutates the graph autonomously from heuristics whose false-positive rate is
  unknown.
- **Decision:** Split the capability into three phases and ship only Phase 1:
  1. **analyzer → proposals** (this PR): read-only except a single rolling Graph
     Review issue; evidence-cited, keyed, human-dispositioned proposals.
  2. **`/graph-review` actioner** (later): apply one accepted proposal.
  3. **scheduling / auto class** (later): run on a cadence.
  Phase 1's explicit deliverable is *measurement*: proposal quality and
  false-positive rate against Colin's judgment, gathered from the accept/dismiss
  ratio on the ledger, gates whether Phase 2 is built at all.
- **Rejected — one detect-and-fix pass (steelman):** it is fewer moving parts
  and closes the loop immediately; a human would not have to hand-apply accepted
  proposals. Rejected because it inverts the risk order: it grants autonomous
  graph mutation *before* the heuristics have earned trust, and graph mutations
  (wrong edge, wrong merge) are expensive to reverse and corrupt the very signal
  the scheduler gates on. Measuring first is cheap; un-merging two tickets is
  not. This mirrors the repo's standing "verification before autonomy" posture
  (triage, AgDR-006/007).
- **Rejected — comments-on-each-ticket instead of one ledger (steelman):** a
  proposal comment on the affected ticket is closer to where a human acts.
  Rejected because it scatters state (no single place to read the accept/dismiss
  ratio), re-notifies on every re-run, and mutates other tickets — violating the
  read-only boundary. One rolling issue is the whole audit surface and the only
  write.
- **Blast radius:** additive. New module `orchestrator/graph_review.py`, one new
  public transport method on the tracker, a documented CLI, and the two design
  docs. No scheduler/runner/workflow change; no new dispatch state; existing
  behavior untouched.
- **Weakest point (accepted):** Phase 1's value depends on Colin actually
  dispositioning proposals on the ledger — an abandoned ledger yields no quality
  signal and Phase 2 stays blocked. And the heuristics (esp. `merge`/`split`)
  will produce false positives; the refute sub-check and the conservative
  "drop-on-doubt" default are the mitigation, but the FP rate is genuinely
  unknown until the ledger has run against a real board. That uncertainty is the
  point of shipping Phase 1 alone rather than a hidden risk.
