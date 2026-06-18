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
- [docs/plans/2026-06-17-sb-worker-loop.md](docs/plans/2026-06-17-sb-worker-loop.md) — M0 Plan 3-A (IMPLEMENTED) — worker loop + subagent protocols + `sb release`
- [docs/ROADMAP.md](docs/ROADMAP.md) — milestone status + the Plan 3 A/B/C/D decomposition, runway, and deferred work (the planning detail; keep it here, not in this file)

## State (2026-06-17)

- Branch `design/switchboard-v2` is the integration line (PR #1 merged Plan 2 into it); `main` holds only the v1 baseline; a commit hook blocks direct main commits
- M0 Plan 1 + Plan 2 complete (engine core; operator surfaces). Plan 3 decomposed into A/B/C/D (see [ROADMAP](docs/ROADMAP.md)); HDR-012 recorded (deliberation is a separate front-end layer, coupled only via the plan/goal artifact)
- **M0 Plan 3-A IMPLEMENTED** (2026-06-17, 135 tests green): `/sb-work` worker-loop skill + task/verifier prompt protocols (`.claude/skills/sb-work/`), `sb release` (infra-requeue, attempts unchanged), `sb/loopledger.py` (token-free ledger + productive/churn diagnostic). Skill owns all git; engine stays git-free. Prompt protocols are reviewed-not-tested by design (exercised live in D)
- Next: B can finalize against A's real artifacts; then A-planner + A-continuation before the D exit bar (see ROADMAP)
- Engine surface (Plan 1+2+3-A): `sb init|seed|claim|file-result|release|spawn|requeue-stale|query|heartbeat|status|brief|stamp|notify`; exit codes 0 ok / 2 held / 3 nothing-to-claim
- `rabbit_guard.py` is a v1 leftover — do NOT wire to the new layout; sub-plan B replaces it (gate.py was replaced by `sb brief`/`sb stamp` in Plan 2)
- Hard invariants (each has pinning tests — keep it that way, see PHI-034): write-before-move into claimable lanes; attempts count task failures only, never infra (`sb release` and stale-requeue preserve attempts); only a verifier verdict reaches done; every phase ends at a GATE task; seeds all-or-nothing; `sb stamp` completes the GATE (paused→done) — the only thing that unblocks the next phase; quota is advisory, never gates a claim (HDR-011); the digest carries pending-review AgDRs (HDR-010 tier-2 channel)

## Conventions

- Every engine write passes `sb/validate.py` (jsonschema choke point); new schema fields = version bump
- Decision records: ADR/AgDR (agent), HDR (human), SDR (synthesis) in top-level `decisions/`; append-only `feedback` for amendments
- Tests: pytest, `lay` fixture (tmp dir), `tests/helpers.make_task`; TDD per plan tasks
- Escalation: substance-tiered per HDR-010 — interrupt only on contestable substance; flag-async otherwise; never ceremony prompts
