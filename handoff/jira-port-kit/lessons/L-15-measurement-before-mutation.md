# L-15 — New autonomy ships a measurement phase before a mutation phase

**Context.** Switchboard wanted a "graph review" capability: reconcile the
board's latent structure (implicit dependencies, mis-filed tickets) with its
enforced structure. The tempting shape was one pass that detects AND fixes.
It shipped instead as phases: Phase 1 read-only analyzer producing a
proposals ledger (keyed, evidence-cited, human-dispositioned); Phase 2 an
actioner that applies *one accepted proposal*; Phase 3 scheduled/auto — each
phase gated on the previous phase's measured quality.

**Why.** Detect-and-fix inverts the risk order: it grants autonomous
mutation *before* the heuristics have earned trust, and graph mutations are
expensive to reverse. Phase 1's explicit deliverable is measurement — the
accept/dismiss ratio against human judgment IS the false-positive rate,
read directly off the ledger. Rejected alternative worth remembering:
scattering proposals as comments on affected tickets — it spreads state,
spams notifications, and quietly violates the read-only boundary. One
rolling ledger is the whole audit surface.

**Known failure mode.** The value depends on humans actually dispositioning
proposals. An abandoned ledger produces no quality signal and blocks the
autonomy it was supposed to earn. With a team, assign the ledger an owner.

**Lesson.** "Verification before autonomy" applies to the system's own
capabilities, not just tickets. Every new agent power over shared state
starts read-only, produces auditable proposals, and graduates on measured
false-positive rate — not on demo quality.

**Portable.** Entirely — arguably more important at L&W, where the shared
board belongs to a team and autonomous mutations have N stakeholders.
