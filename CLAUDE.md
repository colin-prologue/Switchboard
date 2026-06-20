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
- [docs/plans/2026-06-18-sb-guards-quota.md](docs/plans/2026-06-18-sb-guards-quota.md) — M0 Plan 3-B (IMPLEMENTED) — guards + quota detector + monitor; includes errata commits (plan patched when the implementer caught test/logic bugs)
- AgDRs: ADR-001 (guard logic in tested `hooks/` package, not `sb/`) — still `pending-review`; ADR-002 (`.switchboard/` upward discovery, **approved** with a director-required containment cap on the walk, `_MAX_UP=16`); ADR-003 (guard arms on 2nd trip OR exhausted nudge budget — **approved**: err toward stopping); ADR-004 (intervention-learning loop — `proposed`, director-directed, lands with the oversight layer)
- [docs/ROADMAP.md](docs/ROADMAP.md) — milestone status + the Plan 3 A/B/C/D decomposition, runway, and deferred work (the planning detail; keep it here, not in this file)

## State (2026-06-18)

- Branch `design/switchboard-v2` is the integration line (PR #1 merged Plan 2 into it); `main` holds only the v1 baseline; a commit hook blocks direct main commits
- M0 Plan 1 + Plan 2 complete (engine core; operator surfaces). Plan 3 decomposed into A/B/C/D (see [ROADMAP](docs/ROADMAP.md)); HDR-012 recorded (deliberation is a separate front-end layer, coupled only via the plan/goal artifact)
- **M0 Plan 3-A IMPLEMENTED** (140 tests at landing): `/sb-work` worker-loop skill + task/verifier prompt protocols (`.claude/skills/sb-work/`), `sb release` (infra-requeue, attempts unchanged), `sb block` (synthesized blocked result → paused-for-human when a subagent returns no result file; the B deny→blocked contract), `sb/loopledger.py` (token-free ledger + productive/churn diagnostic). Skill owns all git; engine stays git-free. Prompt protocols are reviewed-not-tested by design (exercised live in D)
- **M0 Plan 3-B IMPLEMENTED** (2026-06-18, 171 tests green): token-free safety layer — `hooks/sb_guard.py` (deterministic tripwire guard, per-subagent state by `agent_id`, two-strike nudge→deny), `hooks/sb_quota.py` (rate-limit detector → `quota.json`, advisory only), `hooks/sb_monitor.py` (launchd/cron token-free liveness/quota/notify + early-churn detector reading A's loop-ledger). v1 `rabbit_guard.py` deleted (paid reviewer gone; verification is A's lane). No engine verbs added; logic lives in the tested `hooks/` package (ADR-001). NOT yet built: HDR-010 escalation routing (C); per-task budget wiring (deferred)
- Next: A-planner + A-continuation, then C (HDR-010 escalation), then the D exit bar (see ROADMAP). Pending-review AgDRs ADR-001/002/003 await a human gate
- Engine surface (Plan 1+2+3-A): `sb init|seed|claim|file-result|release|block|spawn|requeue-stale|query|heartbeat|status|brief|stamp|notify`; exit codes 0 ok / 2 held / 3 nothing-to-claim. B adds no verbs — it is hooks + a monitor wrapping existing read verbs
- Hard invariants (each has pinning tests — keep it that way, see PHI-034): write-before-move into claimable lanes; attempts count task failures only, never infra (`sb release` and stale-requeue preserve attempts); only a verifier verdict reaches done; every phase ends at a GATE task; seeds all-or-nothing; `sb stamp` completes the GATE (paused→done) — the only thing that unblocks the next phase; quota is advisory, never gates a claim (HDR-011); the digest carries pending-review AgDRs (HDR-010 tier-2 channel)

## Conventions

- Every engine write passes `sb/validate.py` (jsonschema choke point); new schema fields = version bump
- Decision records: ADR/AgDR (agent), HDR (human), SDR (synthesis) in top-level `decisions/`; append-only `feedback` for amendments
- Tests: pytest, `lay` fixture (tmp dir), `tests/helpers.make_task`; TDD per plan tasks
- Escalation: substance-tiered per HDR-010 — interrupt only on contestable substance; flag-async otherwise; never ceremony prompts
