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

## State (2026-06-15)

- Branch `design/switchboard-v2` is the integration line; `plan/sb-operator-surfaces` (off it) holds the drafted Plan 2 + HDR-011 (unpushed; `main` holds only the v1 baseline; a commit hook blocks direct main commits)
- M0 Plan 1 complete: full `sb` engine, 84 tests green (`.venv/bin/pytest -q`)
- M0 Plan 2 complete: operator surfaces (brief/stamp/status/notify) implemented and tested; gate.py retired
- Engine surface (Plan 1+2): `sb init|seed|claim|file-result|spawn|requeue-stale|query|heartbeat|status|brief|stamp|notify`; exit codes 0 ok / 2 held / 3 nothing-to-claim
- `rabbit_guard.py` is a v1 leftover — do NOT wire to the new layout; Plan 3 replaces it (gate.py was replaced by `sb brief`/`sb stamp` in Plan 2)
- Hard invariants (each has pinning tests — keep it that way, see PHI-034): write-before-move into claimable lanes; attempts count task failures only, never infra; only a verifier verdict reaches done; every phase ends at a GATE task; seeds all-or-nothing; `sb stamp` completes the GATE (paused→done) — the only thing that unblocks the next phase; quota is advisory, never gates a claim (HDR-011); the digest carries pending-review AgDRs (HDR-010 tier-2 channel)

## M0 remaining work

**Plan 3 — judgment layer (after Plan 2):**
- `/sb-work` skill: claim → dispatch task subagent (tier via tiers.json model override) → file-result loop; quota backoff; heartbeats
- Subagent prompt protocols: task (worktree on context.branch, AgDR-instead-of-prompt with steelman + blast radius), verifier, planner (planning is a task type)
- Tripwire hooks (rabbit_guard v2, deterministic only, no API calls)
- HDR-010 requirements: three-tier escalation in the worker skill; independent fresh-context agent judges AgDR tier assignments (self-assessment is bootstrap-only)
- HDR-011 requirements: rate-limit detection is a deterministic PostToolUse hook (token-free) that writes `.switchboard/quota.json`; an external token-free monitor (cron'd `sb status --emit`/`sb notify`, no model calls) surfaces quota + liveness even when the whole fleet is capped/dead — also covers silent session death (spec §11 #3). No Anthropic API exists for subscription 5h/weekly usage; signals are reactive 429 + optional OTEL token counters.
- M0 exit bar: 2-phase toy plan end-to-end with a research-handoff continuation, human stamps the gate
- M0 research tasks: idle-poll tuning (sb claim --wait vs self-pacing); subagent budget enforcement

**Deferred (explicitly):** packaging (schemas are editable-install-only — comment in sb/validate.py), dag.all_edges skipping done/failed lanes, embeddings retrieval, nexus, multi-machine substrate.

## Conventions

- Every engine write passes `sb/validate.py` (jsonschema choke point); new schema fields = version bump
- Decision records: ADR/AgDR (agent), HDR (human), SDR (synthesis) in top-level `decisions/`; append-only `feedback` for amendments
- Tests: pytest, `lay` fixture (tmp dir), `tests/helpers.make_task`; TDD per plan tasks
- Escalation: substance-tiered per HDR-010 — interrupt only on contestable substance; flag-async otherwise; never ceremony prompts
