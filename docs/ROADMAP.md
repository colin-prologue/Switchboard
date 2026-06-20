# Switchboard Roadmap

Living planning doc for milestone work. CLAUDE.md links here for the
decomposition detail and carries only the terse current state. Update the
status line of a sub-plan when it lands; promote durable design decisions into
specs/ADRs rather than narrating them here.

## Milestone status

| Milestone | What | Status |
|---|---|---|
| M0 Plan 1 | `sb` engine core (lanes, leases, claims/wait, spawn, seed, query) | **EXECUTED** — 84 tests at landing |
| M0 Plan 2 | operator surfaces (`brief`/`stamp`/`status`/`notify`); `gate.py` retired | **EXECUTED** (merged via PR #1) — 122 tests |
| M0 Plan 3-A | worker loop + subagent protocols + `sb release` + `sb block` | **IMPLEMENTED** 2026-06-17 — 140 tests |
| M0 Plan 3 A-planner | `sb seed --goal` + planner protocol | spec'd in 3-A doc §7; **not built** |
| M0 Plan 3 A-continuation | research-handoff continuation chain | spec'd (worker-loop §3.3); **not built** |
| M0 Plan 3-B | guards + quota/liveness | **IMPLEMENTED** 2026-06-18 — 171 tests |
| M0 Plan 3-C | HDR-010 escalation layer | design sketch; finalize after A |
| M0 Plan 3-D | M0 exit bar (acceptance) | **not built** |

## Plan 3 — judgment layer (decomposed into four sub-plans)

HDR-012 keeps the deliberation front-end out of scope — that is a separate
post-M0 track.

- **A — worker loop + subagent protocols** (the execution spine). **IMPLEMENTED**
  on `design/switchboard-v2` (135 tests at landing; 140 after the deny→blocked
  follow-on below).
  Plan: [docs/plans/2026-06-17-sb-worker-loop.md](plans/2026-06-17-sb-worker-loop.md);
  spec: [docs/specs/2026-06-16-sb-worker-loop-design.md](specs/2026-06-16-sb-worker-loop-design.md).
  Delivered: `/sb-work` skill (`.claude/skills/sb-work/SKILL.md` + `task-protocol.md`
  + `verifier-protocol.md`) — long-running interactive loop (claim --wait →
  provision worktree → dispatch fresh subagent at tier → file-result → teardown;
  heartbeat per task pass); `sb release` (infra-requeue, attempts unchanged);
  `sb/loopledger.py` (token-free loop-ledger + productive/churn diagnostic);
  stub-dispatcher integration test. `max_loop_iterations` is a **diagnostic
  checkpoint that pauses, not a kill**; idle waits are not loop iterations
  (heartbeat only). Skill owns all git; engine stays git-free.
  - *Reviewed-not-tested (by design, spec §6):* the task/verifier **prompt
    protocols** — get their live exercise in D.
  - *Follow-on (hardening, needed by B's deny→blocked contract) — **DONE**
    2026-06-17:* `sb block <id> --reason` synthesizes a `blocked` result and
    routes the task to paused-for-human, for when a dispatched subagent returns
    with **no valid result file** (guard-forced stop or crash). Rejects verify
    tasks (a crashed verifier is infra → `release`) and tasks that already have a
    result file. SKILL.md step 7 wires the branch (task→`block`, verifier→
    `release`). This is the guarantee that a guard-forced stop (B §3) pauses for
    human instead of `sb file-result` raising `FileNotFoundError`.
  - *Follow-on:* `max_loop_iterations` is a skill default (200), not in
    `paths.DEFAULT_CONFIG`. Add to config if operator-tunability is wanted.
- **A-planner** (small follow-on before D): `sb seed --goal` + planner prompt
  protocol (planning is a task type → writes `plans/<id>.json` + SDR). The loop
  dispatches a planner identically; only the entry point and SDR/plan emission
  are new.
- **A-continuation** (small follow-on before D): research-handoff chain —
  `paused_for_research` result outcome → `sb spawn` (exists) → continuation task
  depending on it (worker-loop spec §3.3). `sb spawn` exists; this needs a
  result-outcome + re-enqueue, so it is real engine work. D requires exercising
  one chain.
- **B — guards + quota/liveness** (independent of A; token-free). rabbit_guard v2
  deterministic tripwire hooks (repeat-call/repeat-error/no-progress/budget;
  first trip nudge, second forces `blocked`); HDR-011 rate-limit PostToolUse
  detector → `.switchboard/quota.json`; external token-free monitor (cron'd
  `sb status --emit`/`sb notify`, no model calls) for quota + liveness +
  silent-session-death (v2 design §11 #3). Owns the sharp early no-progress
  detector A's coarse cap defers to. Research task: subagent budget enforcement
  (resolved: hooks, not loop checks). **Spec finalized 2026-06-17 against A's
  real artifacts** — the deny→blocked contract (worker synthesizes `blocked`),
  per-task budget (deferred; global defaults for M0), and the early-churn
  detector (extends `sb/loopledger.py`, reads the real ledger schema) are now
  pinned. **IMPLEMENTED 2026-06-18** ([plan](plans/2026-06-18-sb-guards-quota.md),
  171 tests): `hooks/sb_guard.py` + `hooks/sb_quota.py` + `hooks/sb_monitor.py`;
  v1 `rabbit_guard.py` deleted; ADR-001/002/003 recorded (pending-review). Plan
  carried 2 errata commits (guard test/logic bugs caught by the implementer
  subagent's spec-compliance refusal — the two-stage review working as designed).
- **C — HDR-010 escalation layer** (depends on A; uses Plan 2 notify). three-tier
  interrupt/flag-async/record-silent routing; independent fresh-context agent
  judges AgDR tier assignments (self-assessment is bootstrap-only). Open Qs
  (finalize now that A is real): when the tier judge runs (per-AgDR vs batched),
  bootstrap handoff.
  - **Intervention-learning loop** (director-directed 2026-06-19; ADR-004,
    proposed): when the guard hard-stops a thrashing agent and a human resolves
    the resulting paused-for-human task, capture the resolution as a tagged
    decision record so it flows into the existing `sb query` grounding and helps
    future similar tasks avoid the same dead-end. Lean MVP reuses the decision
    corpus + grounding loop already built; a small capture step at the
    blocked→paused_for_human boundary is the only new machinery. Active
    pre-emptive failure-signature matching is the heavier fallback if passive
    grounding misses repeats. Builds on B's guard escalation (ADR-003).
- **D — M0 exit bar** (validates A+B+C). 2-phase toy plan end-to-end with a
  research-handoff continuation, human stamps the gate. Acceptance test, not a
  feature.

**Self-build runway note:** the fleet cannot safely self-build until A+B are
built and D passes under human supervision (PHI-030: verification before
autonomy). So C and M1 are the first genuinely autonomous-executable work; specs
written ahead of A were runway, and B/C specs may need revision now that A's
emergent behavior is real.

**M0 research tasks:** idle-poll tuning (`sb claim --wait` vs self-pacing, v2
design §3.4); subagent budget enforcement (in B).

## Known follow-ups

- **Fully race-free claim+lease** (Codex C3, partially mitigated 2026-06-20):
  `claim_one` now writes the lease before the body, narrowing — but not closing —
  the window where a concurrent `requeue-stale` can bounce a just-claimed task
  back to queued (duplicate dispatch). **Closure condition:** before anything
  *auto-runs* `requeue-stale` (a scheduled sweep / future monitor wiring), make
  the claim+lease fully atomic. Today the sweep is manual-only, so the residual
  window is latent.

## Deferred (explicitly)

Packaging (schemas are editable-install-only — comment in `sb/validate.py`),
`dag.all_edges` skipping done/failed lanes, embeddings retrieval, nexus,
multi-machine substrate.
