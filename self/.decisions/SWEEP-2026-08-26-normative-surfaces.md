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

## Second correction — the sweep's scope conclusion does not survive

Two further findings on the same PR, both in `README.md`, after the first
correction claimed both README defects were fixed:

- **The documented quick start produced a ticket that is never dispatched.**
  `register-project.sh:51` defaults `--stance prototype`; `new-ticket.sh:23`
  defaults `--entry triage`; `prototype` does not dispatch `triage`. Following
  the README verbatim files a dead ticket under a line promising "the
  orchestrator picks it up". Filed as **#176** — the tools carry the trap, and
  the doc warning added here is a memory aid, not a fix.
- **The handoff contract still stated a constant target.** `README.md:151` said
  the orchestrator performs "the single transition to `status:human-review`" —
  contradicting `tracker.handoff_label`, and contradicting a paragraph added
  earlier in this same PR.

**The "drift was not general" conclusion is withdrawn.** It survived one
correction and not two. Four defects in `README.md` alone, all stance-blindness,
all from the same 2026-08-15 propagation gap — that is a pattern, not a file
that fell out of a set. The honest summary is: **`SETUP.md` and
`METHODOLOGY.md` were updated for the stance ladder; `spec/SPEC.md` and
`README.md` were not**, and the second sweep found the fourth defect only after
review found the second and third.

Three rounds of review on a sweep whose subject is "documents that stopped
describing the system" found three more instances in that sweep's own output.
The method — read for claims, check each against code — works. Doing it once
does not.

## Not found

No stale scripts, flags, config values, or state vocabulary in `SETUP.md` or
`METHODOLOGY.md`. `README.md` was **not** clean — see the correction above. `README.md`'s Codex section correctly records
the canary retirement (#155). `SETUP.md`'s claim that a restart refunds each
issue's session budget is accurate and load-bearing — it was observed twice on
2026-08-25/26.

> ~~This matters for calibrating the next sweep: doc drift here was **not**
> general. It was one file that fell out of a propagation set, which is a cheaper
> problem to watch for than "the docs rot".~~
>
> **Withdrawn 2026-08-26** — see the corrections above. Struck rather than
> deleted, per the supersession rule: what was believed, and why it changed, is
> what makes the next conflict legible. It was wrong in both directions — two
> files fell out of the set, not one, and README's defects were found only by
> three successive rounds of adversarial review.

## What follows

- **Two of four** normative surfaces fell out of the 2026-08-15 propagation set,
  not one: `SPEC.md` and `README.md`. `METHODOLOGY.md` and `SETUP.md` were
  updated. A propagation checklist is therefore a *four*-item memory aid, which
  both sweeps argue will fail the same way.
- The thing that actually worked is not in this record's method at all. Every
  README defect — including the one with a user-facing consequence (#176) — was
  found by **adversarial re-reading by a party who was not the author**, over
  three successive rounds, after this sweep had declared the file clean. Two of
  those rounds found defects the sweep itself had just introduced.
- So the honest recommendation is *not* "adopt the sweep procedure". It is that a
  single pass by a single reader reproduces the defect class it is hunting.
  Whatever gets encoded should be about who reads, not about what list they hold.
