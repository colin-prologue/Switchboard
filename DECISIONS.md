# Decisions

Human-readable index over the structured records in `.decisions/`. Each entry is a
one-liner; the JSON beside it holds options, research, evidence, and the feedback trail.
This file is the early-stage substitute for the rich timeline/graph view — kept scannable
on purpose so it stays useful, not a wall of prose. The schema is normalized so a future
build step can render timeline + level-of-resolution + search straight from the JSON
without touching this file.

**Conventions** — IDs: `ADR` agent · `HDR` human · `SDR` synthesis. Status in `[brackets]`.
Filter mentally (or, later, programmatically) by **tag**, **level**, and **phase**.

---

## Feature: Caching Layer

### Plan
- **SDR-012** — Decompose into 3 phases with tiered model routing `[approved]`
  Fable at the design gate, Opus implements, Haiku migrates. *tags: decomposition, model-routing*
  → [SDR-012.json](.decisions/SDR-012.json)

### Implementation
- **ADR-047** — Cache uses immutable state to eliminate race conditions `[feedback-incorporated]`
  Immutable snapshots beat mutable-with-locks (which deadlocked at 3h under load).
  *Feedback (colin/EM): revisit if we adopt thread-local caching.* — depends on SDR-012
  → [ADR-047.json](.decisions/ADR-047.json)

---

## How a decision flows

1. **Agent pauses** at a choice point, does passive research, emits an `ADR` (`confidence` set).
2. **Low/medium confidence or shared-architecture impact** → `status: pending-review`; the
   orchestrator surfaces it at the PR gate. High-confidence + local → may auto-`approve`.
3. **Human reviews** the synthesized brief, appends a `feedback` entry → status moves to
   `approved` / `feedback-incorporated`. The annotation lives on the record forever.
4. **Future sessions** query by tag/level before deciding — grounding new choices in this
   team's precedent rather than re-deriving. Superseded decisions stay, linked via
   `supersedes` / `superseded_by`, so the lineage of how thinking evolved is never lost.
