# Switchboard — Project Memory

Deterministic file-queue engine + (upcoming) Claude Code skills for multi-agent
orchestration on subscription billing. One person directs many agents; decisions
are durable records; humans review at PR gates.

## Authoritative artifacts (read in this order)

- [MISSION.md](MISSION.md) — why this exists; principles every decision tests against
- [docs/specs/2026-06-12-switchboard-v2-design.md](docs/specs/2026-06-12-switchboard-v2-design.md) — the v2 design (platform-native; supersedes ARCHITECTURE.md's v1 framing where they conflict)
- [decisions/](decisions/) — HDR-001..011, file-per-record. HDR-006 (runtime), HDR-007 (substrate), HDR-008 (oversight), HDR-010 (substance-tiered escalation + independent tier judgment, bootstrap exception in its feedback), HDR-011 (quota/liveness via deterministic hook + external token-free monitor; quota is advisory, never a claim gate)
- [docs/plans/2026-06-12-sb-engine-core.md](docs/plans/2026-06-12-sb-engine-core.md) — M0 Plan 1 (EXECUTED; includes errata commits — the plan was patched when reviews found bugs in planned code)
- [docs/plans/2026-06-14-sb-operator-surfaces.md](docs/plans/2026-06-14-sb-operator-surfaces.md) — M0 Plan 2 (EXECUTED) — operator surfaces (brief/stamp/status/notify); branch `plan/sb-operator-surfaces`

## State (2026-06-16)

- Branch `design/switchboard-v2` is the integration line (PR #1 merged Plan 2 into it); `main` holds only the v1 baseline; a commit hook blocks direct main commits
- M0 Plan 1 complete: full `sb` engine, 84 tests green (`.venv/bin/pytest -q`)
- M0 Plan 2 complete (merged): operator surfaces (brief/stamp/status/notify) implemented and tested; gate.py retired; 122 tests green
- Plan 3 decomposed into A/B/C/D (see M0 remaining work). HDR-012 recorded (deliberation is a separate front-end layer, coupled only via the plan/goal artifact). Sub-plan A spec ([docs/specs/2026-06-16-sb-worker-loop-design.md](docs/specs/2026-06-16-sb-worker-loop-design.md)) + B spec ([docs/specs/2026-06-16-sb-guards-quota-design.md](docs/specs/2026-06-16-sb-guards-quota-design.md)) written + approved. Remaining runway: C (design sketch, finalize after A), D (acceptance-criteria doc). Next: writing-plans → subagent-driven execution of A.
- Engine surface (Plan 1+2): `sb init|seed|claim|file-result|spawn|requeue-stale|query|heartbeat|status|brief|stamp|notify`; exit codes 0 ok / 2 held / 3 nothing-to-claim
- `rabbit_guard.py` is a v1 leftover — do NOT wire to the new layout; Plan 3 replaces it (gate.py was replaced by `sb brief`/`sb stamp` in Plan 2)
- Hard invariants (each has pinning tests — keep it that way, see PHI-034): write-before-move into claimable lanes; attempts count task failures only, never infra; only a verifier verdict reaches done; every phase ends at a GATE task; seeds all-or-nothing; `sb stamp` completes the GATE (paused→done) — the only thing that unblocks the next phase; quota is advisory, never gates a claim (HDR-011); the digest carries pending-review AgDRs (HDR-010 tier-2 channel)

## M0 remaining work

**Plan 3 — judgment layer (after Plan 2). Decomposed into four sub-plans (HDR-012 keeps the deliberation front-end out of scope — that is a separate post-M0 track):**

- **A — worker loop + subagent protocols** (the execution spine). Spec written + approved: [docs/specs/2026-06-16-sb-worker-loop-design.md](docs/specs/2026-06-16-sb-worker-loop-design.md). `/sb-work` long-running interactive loop (claim --wait → provision worktree → dispatch fresh subagent at tier → file-result → teardown; heartbeat/iteration); **task** + **verifier** prompt protocols; `sb release` (infra-requeue, attempts unchanged); skill owns all git, engine stays git-free; `max_loop_iterations` is a **diagnostic checkpoint that pauses, not a kill** (token-free loop-ledger + loop-diagnostic classifying productive vs churn). Scope-deferred from A: planner + research-continuation.
- **A-planner** (small follow-on before D): `sb seed --goal` + planner prompt protocol (planning is a task type → writes `plans/<id>.json` + SDR).
- **A-continuation** (small follow-on before D): research-handoff chain — `paused_for_research` result outcome → `sb spawn` (exists) → continuation task depending on it (spec §3.3). D requires exercising one chain.
- **B — guards + quota/liveness** (independent of A; token-free). rabbit_guard v2 deterministic tripwire hooks (repeat-call/repeat-error/no-progress/budget; first trip nudge, second forces `blocked`); HDR-011 rate-limit PostToolUse detector → `.switchboard/quota.json`; external token-free monitor (cron'd `sb status --emit`/`sb notify`, no model calls) for quota + liveness + silent-session-death (spec §11 #3). Owns the sharp early no-progress detector A's coarse cap defers to. Research task: subagent budget enforcement (hooks vs loop checks).
- **C — HDR-010 escalation layer** (depends on A; uses Plan 2 notify). three-tier interrupt/flag-async/record-silent routing; independent fresh-context agent judges AgDR tier assignments (self-assessment is bootstrap-only). Open Qs (finalize after A is real): when the tier judge runs (per-AgDR vs batched), bootstrap handoff.
- **D — M0 exit bar** (validates A+B+C). 2-phase toy plan end-to-end with a research-handoff continuation, human stamps the gate. Acceptance test, not a feature.
- **Self-build runway note:** the fleet cannot safely self-build until A+B are built and D passes under human supervision (PHI-030: verification before autonomy). So C and M1 are the first genuinely autonomous-executable work; specs written ahead of A are runway, but B/C/D specs may need revision once A's emergent behavior is real.
- M0 research tasks: idle-poll tuning (sb claim --wait vs self-pacing, spec §3.4); subagent budget enforcement (in B).

**Deferred (explicitly):** packaging (schemas are editable-install-only — comment in sb/validate.py), dag.all_edges skipping done/failed lanes, embeddings retrieval, nexus, multi-machine substrate.

## Conventions

- Every engine write passes `sb/validate.py` (jsonschema choke point); new schema fields = version bump
- Decision records: ADR/AgDR (agent), HDR (human), SDR (synthesis) in top-level `decisions/`; append-only `feedback` for amendments
- Tests: pytest, `lay` fixture (tmp dir), `tests/helpers.make_task`; TDD per plan tasks
- Escalation: substance-tiered per HDR-010 — interrupt only on contestable substance; flag-async otherwise; never ceremony prompts
