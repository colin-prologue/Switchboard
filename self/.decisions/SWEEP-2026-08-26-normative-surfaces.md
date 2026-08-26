# Sweep 2026-08-26 (b) — normative surfaces

**Scope:** `spec/SPEC.md`, `methodology/METHODOLOGY.md`, `README.md`, `SETUP.md`,
`spec/PROVENANCE.md` — 1467 lines.
**Question asked:** *does this still describe what the system does?*

Prompted by the first sweep's postscript. That pass read `self/.decisions/` and
missed two stale `spec/` claims, one of which codex found on the sweep's own PR.
The distinction that motivated this: a rejection that decays is a stale belief;
a **spec that decays is a stale instruction**, and something downstream
implements it.

## Method

Claims were extracted mechanically by class and each checked against `fa4505e`:

| Class | Checked | Result |
|---|---|---|
| Scripts referenced (`scripts/*.sh`) | 5 | all exist |
| CLI flags documented (`--*`) | 21 | all exist; the 5 non-matching are `uv`/module flags, not script flags |
| Config values and timeouts quoted in prose | 9 | all match code/stance defaults |
| `max_sessions_per_issue` "default 3" | 1 | correct (`workflow.py:540`) |
| `status:*` vocabulary per document | 4 docs | **one gap — see below** |
| Stance-awareness of state claims | 4 docs | **one document blind — see below** |

## Finding: `spec/SPEC.md` §2 predates the stance ladder

`SPEC.md` has been modified **once** since 2026-08-14, and that commit was
`0db98c0` — this sweep's own predecessor, prompted by codex. The stance ladder
(AgDR-039, 2026-08-15), AgDR-043, AgDR-044 and AgDR-045 all landed without
touching it. `README.md`, `SETUP.md` and `METHODOLOGY.md` were each updated;
`SPEC.md` was not.

Three claims, all stating `base`'s configuration as universal:

1. **`active_states`** was given as `["triage", "todo", "in progress"]`, flat.
   `prototype` is `["todo", "in progress", "review"]` — no `triage`, plus
   `status:review`, which `SPEC.md` did not mention anywhere.
2. **The handoff transition** was "the single `status:human-review` transition".
   It is the stance's `tracker.handoff_label` (`types.py:131`), which `prototype`
   sets to `status:review`.
3. **Gate states** were listed as `base`'s four, flat. `prototype` gates only
   `status:human-review`.

All three corrected here.

### The part that matters more than the drift

`METHODOLOGY.md:55` already states claim 2 **correctly** — *"the stance's
`handoff_label` — `status:human-review` by default, `status:review` at
`prototype`"* — while `SPEC.md:137` stated it as a constant.

**Two normative surfaces held contradictory beliefs about one fact, and nothing
noticed.** That is the same second-reader failure `METHODOLOGY.md` describes, and
the same shape as the first sweep's headline (AgDR-030 naming a hazard the other
provider's runner then shipped). It is also the exact defect class #164 fixed in
`status_board.py` — "the board derived every project's rules from one template" —
still live in the document that declares itself authoritative for the bindings.

## Correction 2026-08-26 — this sweep produced a false negative

Codex review of this sweep's own PR found that `README.md:71` marked
`status:triage` as unconditionally **active**, while marking only `status:review`
as stance-dependent. `prototype`'s `active_states` is
`["todo", "in progress", "review"]` — no `triage`. So README carried the *same*
flat-state defect this sweep was written to find, and the audit below recorded it
as accurate.

The lifecycle diagram had it too, and is now labelled as the `base` pipeline.

**The audit's method was the weakness, not its attention.** README was checked by
grepping for the word "stance" and finding it present. That confirms a document
mentions stances somewhere; it cannot confirm every state claim inside it is
qualified. A per-state comparison against each stance's `active_states` would
have caught it — and is what the next pass should do instead.

Both corrected. The finding below stands, but its scope was one file wider than
recorded, and the "not general" conclusion is correspondingly weaker.

## Not found

No stale scripts, flags, config values, or state vocabulary in `SETUP.md` or
`METHODOLOGY.md`. `README.md` was **not** clean — see the correction above. `README.md`'s Codex section correctly records
the canary retirement (#155). `SETUP.md`'s claim that a restart refunds each
issue's session budget is accurate and load-bearing — it was observed twice on
2026-08-25/26.

This matters for calibrating the next sweep: doc drift here was **not** general.
It was one file that fell out of a propagation set, which is a cheaper problem to
watch for than "the docs rot".

## What follows

- The cheap invariant this suggests: when a change updates the state machine,
  `SPEC.md` is in the propagation set with `METHODOLOGY.md`, `README.md` and
  `SETUP.md`. Three of four were updated every time; the fourth was never
  updated once.
- Whether that becomes a checklist item, a test, or nothing is deliberately left
  open. Both sweeps now argue that anything depending on *remembering* fails the
  same way — and a fourth item on a list is a memory aid.
