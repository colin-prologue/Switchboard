# sb Escalation Layer (tier calibration + intervention-learning) — Design (M0, Plan 3 sub-plan C)

**Status:** Approved design 2026-06-23 (pending user review of this file).
Graded depth: lean — an independent leanness review (2026-06-23) collapsed the
originally-separate "tier-judge" dispatch into the existing verifier, so C is
now mostly prompt-protocol plus two small engine touches. Depends on A (the
worker loop + the verifier lane) and uses Plan 2's digest/notify. Implementation
plan to follow via writing-plans.

**Scope:** the judgment layer of M0 — *which* agent decisions reach the operator
and *when*. Two pieces, one plan / two phases:

1. **Tier calibration** (HDR-010): the independent verifier, which already runs
   at every task's completion gate, additionally calibrates each emitted AgDR
   into one of HDR-010's three tiers, expressed as three **notification
   cadences** — immediate / digest / gate-only. The operator stops seeing every
   pending AgDR (today's behavior, which trains rubber-stamping) and instead sees
   the contestable ones promptly and the routine ones only at the gate.
2. **Intervention-learning loop** (ADR-004): `sb resolve` — the (currently
   missing) resolution path for a `paused_for_human` task, which also optionally
   captures the human's resolution as a tagged decision record that flows into
   the existing `sb query` grounding future authors pull.

**Governing decisions/spec:** HDR-010 (three-tier escalation calibrated to
decision substance; the amendment requiring an **independent** tier judge once
Switchboard is functional, "same independence rule as the verification lane"),
ADR-004 (capture human resolutions of guard hard-stops as queryable preventive
records; reuse the decision-corpus + grounding loop, defer active
failure-signature matching), PHI-028 (AgDR-instead-of-prompt), PHI-030
(independence / verification before autonomy), HDR-008 (oversight), v2 design §6
(verification) / §7 (notify). **2026-06-23 independent review** collapsed the
tier-judge into the verifier and confirmed per-task calibration is free once
collapsed.

---

## 1. The decision this implements, and where we start

HDR-010 calibrates escalation to **decision substance**, not action category,
across three tiers:

- **interrupt** (tier 1) — the operator should look *now*: a call that should
  have blocked but didn't (the author proceeded where an independent reader
  judges it contestable enough to stop).
- **flag-async** (tier 2) — contestable but reversible: ping the operator, work
  proceeds, overturn is bounded by a pre-merge branch revert.
- **record-silent** (tier 3) — high confidence, local blast, verified
  reversible: no ping; rides to the gate-review profile.

HDR-010's amendment makes self-assessed tiers **bootstrap-only**: once
Switchboard is functional, tier judgment must come from an independent agent
(fresh context, different model than the author — "same independence rule as the
verification lane").

**Where we start (bootstrap state, today):** task subagents self-assess per
PHI-028 — a hard-escalation domain → a `blocked` result (a true stop at author
time); anything else contestable → a `pending-review` AgDR, then proceed. The
digest (`sb status --emit`) surfaces **every** `pending-review` AgDR. So the
system today behaves as if every pending AgDR were tier-2: no record-silent
filtering, no independent calibration. C closes that.

## 2. Architectural choices (each an AgDR to record)

**C-1 — fold the tier judge into the verifier (do not add a second dispatch).**
The verifier already runs at the completion gate the design needs (a successful
task is set to `awaiting_verification`, not `done`; only a verifier `pass` at a
later loop pass moves it to `done` — `results.py::_route_outcome`/`_apply_verdict`).
It is already the independent reader HDR-010 asks for: `verifier_tier_for`
(`results.py:25`) forces the verifier tier ≠ author tier. HDR-010 requires
independence *from the author*, which the verifier satisfies; it does **not**
require independence from the verifier. Correctness and tier-substance are the
same deep read of the same committed diff, so a separate tier-judge would
re-derive context the verifier already built. → Tier calibration becomes a
**secondary section of `verifier-protocol.md`**, not a new `judge-protocol.md`
and not a second subagent dispatch. *Tradeoff:* one less degree of isolation
(a verifier miscalibrated on correctness could be correlated-miscalibrated on
tier) and slight prompt-attention load; mitigated by ordering the protocol
verdict-first, calibration-second, and by the fact this is reviewed-not-tested
and exercised live in D regardless.

**C-2 — the author does not self-assess a tier.** Because the verifier now
*always* runs for any AgDR-emitting task and is the sole tier authority,
self-tiering (with a judge-override + feedback audit trail) is redundant
machinery. The author writes the AgDR as it does today; the verifier tiers it.
→ **C adds nothing to `task-protocol.md`.** Untiered AgDRs default to
**flag-async** (fail-safe toward visibility — an un-calibrated decision pings,
it never silently hides).

**C-3 — interrupt is an immediate ping, not a queue block.** Routing a task to
`paused_for_human` on interrupt would require a third verify disposition, a
result-schema bump (0.2.0→0.3.0), and an `_apply_verdict` branch. It buys little:
the emitting task's own dependents are *already* fenced (it sits in
`awaiting_verification` until the verifier passes), and the **phase GATE already
forces the operator to review every pending AgDR before the next phase runs** —
so a true block only fences *same-phase siblings* for the window between the AgDR
and the gate, which is bounded and revertible pre-merge (HDR-010 tier-2's own
escape hatch). → interrupt fires an **immediate `sb notify`** (now, not at the
next digest cycle); the task still proceeds to `done`. The three tiers become
three **cadences**: immediate / digest / gate-only. *Tradeoff:* this softens
HDR-010's literal tier-1 from "blocks the queue" to "forces immediate
attention"; the phase gate is the backstop. (Director chose this over the
true-block variant, 2026-06-23.)

**C-4 — tier is represented via the `tags` expansion joint, not a schema field.**
A persisted marker *is* required (interrupt is already distinguishable — the
operator is pinged; but flag-async vs record-silent are both `pending-review`
AgDRs on a `done` task and `confidence` cannot separate them). The
decision-record schema declares `tags` an **expansion joint** ("loose by design,
grows without a bump"), so the verifier writes `escalation:record-silent` /
`escalation:interrupt` onto the AgDR's `tags`. → **no `tier` field, no 0.4.0
schema bump** — consistent with ADR-007's no-schema-change precedent. *Tradeoff:*
routing parses a tag convention rather than a typed enum; the controlled
vocabulary lives in this spec + the protocol, not the schema. Promote to a
first-class field only if the convention proves insufficient (deferred).

## 3. What changes (components)

- **`.claude/skills/sb-work/verifier-protocol.md`** (reviewed-not-tested; live in
  D) — a new secondary section: *after* the pass/fail verdict, if the task under
  verification emitted AgDRs (`decisions_emitted` non-empty), the verifier reads
  each AgDR and calibrates it against HDR-010's substance criteria
  (confidence × blast-radius × reversibility). It acts only on non-default
  tiers: add `escalation:record-silent` to demote, `escalation:interrupt` to
  promote; leave flag-async untagged. It writes the tag onto the AgDR record in
  the worktree and commits it on the same branch as the AgDR (tag travels with
  the record). The verifier's own emitted AgDRs, if any, are not self-calibrated
  (no recursion) — they default to flag-async.
- **`sb/digest.py`** (TDD) — partition `pending-review` AgDRs by the `escalation:`
  tag into three buckets: `interrupt_agdrs` (high-priority), `pending_agdrs`
  (flag-async + untagged — the current ping list), `record_silent_agdrs`
  (gate-profile only, not pinged). `sb notify` pings on `interrupt_agdrs` +
  `pending_agdrs`; the gate-review profile shows all three.
- **`.claude/skills/sb-work/SKILL.md`** (reviewed-not-tested) — the verify-pass
  step, after filing the verdict, checks whether the verifier tagged any AgDR
  `escalation:interrupt`; if so it fires `sb notify` immediately (the existing
  verb, no new engine surface) so the ping does not wait for the next digest
  cycle.
- **`sb resolve <id>` (engine verb, TDD)** — Phase 2. Transitions a
  `paused_for_human` task back to `queued` for a fresh attempt (the human has
  unblocked the wedge), reusing the validated write-before-move path. It
  **optionally** writes a tagged resolution decision record (cause / fix /
  one-line preventive rule, `tags:["intervention-resolution", ...]`) using the
  existing decision-record schema (no schema change — ADR-004's chosen shape).
  The record flows into the `sb query` grounding the retrying author pulls. The
  record is *optional* on purpose: forcing a structured write on every un-pause
  would re-train the rubber-stamping HDR-010 fights.

The `sb` engine surface gains exactly **one verb** (`sb resolve`) and one
read-side digest change. No schema bump (decision or result). No new task kind,
no new result outcome.

## 4. Data flow

**Tier calibration (Phase 1):**
author emits AgDR (untagged) → task → `awaiting_verification` → verify loop pass
→ verifier verdict (correctness) → verifier calibrates AgDR tier, tags + commits
→ task → `done` (always, if correctness passes) → if `interrupt` tagged, skill
fires immediate `sb notify` → digest/notify route by tag (immediate / flag-async
ping / record-silent silent) → operator reviews all tiers at the phase GATE
(`sb stamp`).

**Intervention-learning (Phase 2):**
guard hard-stop or `sb block` → task `paused_for_human` → human investigates,
fixes the wedge → `sb resolve <id>` (optionally records cause/fix/rule) → task
re-queued → a future author's `sb query` grounding surfaces the resolution
record → the dead-end is avoided. (This is the ADR-004 loop closing back to the
author's grounding step.)

## 5. Independence & bootstrap

- The verifier is the **sole** tier authority (C-2); its tier-≠-author guarantee
  (C-1) is the independence HDR-010's amendment requires.
- **Bootstrap / fail-safe:** any AgDR the verifier did not tag (verifier crash →
  infra `release` → re-verify; the verifier's own AgDRs; a future path with no
  verifier) defaults to **flag-async** — visible, never silently suppressed.
- **No recursion:** the verifier does not calibrate its own emitted AgDRs.

## 6. Error handling & edge cases

- **Verifier crash** during a verify pass is infra, not human-blockable — the
  existing contract stands (`release`, re-queue for another verifier); attempts
  on the original task are untouched. Calibration simply happens on the
  successful verify.
- **Task emits no AgDR** → the verifier's calibration section is a no-op.
- **Planner SDRs / `decision_ref`** (A-planner) — a planner task that emits a
  decision record is calibrated by the same path (it goes through a verifier).
- **AgDR visibility is checkout/merge-dependent (inherited seam).** The digest
  reads `repo/decisions` on the checkout it runs in; AgDRs (and their tags) are
  committed on the phase branch. This is a **pre-existing** property of HDR-010's
  tier-2 channel, not introduced by C — the tag travels with the record through
  whatever path already surfaces pending AgDRs to the operator. C does not change
  it; confirm the operator/monitor's checkout assumption when wiring D, and treat
  closing any gap as Plan-2/monitor work, not C.

## 7. Testing

- **TDD (engine):** `sb/digest.py` tier partitioning (fixtures: AgDRs tagged
  interrupt / record-silent / untagged → correct buckets; notify ping list
  excludes record-silent); `sb resolve` (paused_for_human → queued; optional
  record written + schema-valid; rejects non-paused tasks; preserves the
  write-before-move invariant).
- **Reviewed-not-tested (prompt protocols, by design — live in D):** the
  `verifier-protocol.md` calibration section and the SKILL immediate-notify wire.
  D's 2-phase toy plan must exercise an AgDR through calibration end-to-end with
  the guard hooks wired.
- **Invariants to pin (PHI-034):** untagged pending AgDR = flag-async (fail-safe
  visible); record-silent is excluded from the notify ping but present in the
  gate profile; `sb resolve` preserves write-before-move and the attempts
  accounting it chooses; the digest remains a pure function of disk state.

## 8. Non-goals / deferred

- **Active pre-emptive failure-signature matching** (ADR-004's heavier fallback)
  — only if passive grounding demonstrably misses repeats once real failure data
  exists.
- **A first-class `tier` schema field / 0.4.0 bump** — deferred unless the tag
  convention proves insufficient (C-4).
- **True queue-blocking interrupt** (`verdict: escalate` + result-schema bump)
  — explicitly rejected for M0 (C-3); revisit only if the phase-gate backstop
  proves inadequate in practice.
- **Fixing AgDR→digest checkout visibility** — inherited Plan-2/monitor seam (§6).
- **`sb resolve` marking a task done-by-human-manual** — out of scope; resolve
  re-queues for a grounded retry.

## 9. AgDRs to record at the gate

- **C-1** fold tier-judge into the verifier (independence via `verifier_tier_for`).
- **C-2** author does not self-tier; verifier is sole authority; untagged = flag-async.
- **C-3** interrupt = immediate ping, not a queue block (director-chosen).
- **C-4** tier via the `tags` expansion joint, not a schema field (ADR-007-class
  representation call).

## 10. Phasing

- **Phase 1 — tier calibration:** verifier-protocol section + digest partition +
  immediate-notify wire. The HDR-010 closure condition D must exercise.
- **Phase 2 — `sb resolve`:** the engine verb + optional resolution record. It is
  independent of Phase 1 (touches a different boundary) and lower-risk (one verb,
  reusing the existing `paused_for_human` flow) — it **may land first**.
