# Switchboard — Project Memory

Deterministic file-queue engine + (upcoming) Claude Code skills for multi-agent
orchestration on subscription billing. One person directs many agents; decisions
are durable records; humans review at PR gates.

## Authoritative artifacts (read in this order)

- [MISSION.md](MISSION.md) — why this exists; principles every decision tests against
- [docs/specs/2026-06-12-switchboard-v2-design.md](docs/specs/2026-06-12-switchboard-v2-design.md) — the v2 design (platform-native; supersedes ARCHITECTURE.md's v1 framing where they conflict)
- [decisions/](decisions/) — HDR-001..010, file-per-record. HDR-006 (runtime), HDR-007 (substrate), HDR-008 (oversight), HDR-010 (substance-tiered escalation + independent tier judgment, bootstrap exception in its feedback)
- [docs/plans/2026-06-12-sb-engine-core.md](docs/plans/2026-06-12-sb-engine-core.md) — M0 Plan 1 (EXECUTED; includes errata commits — the plan was patched when reviews found bugs in planned code)

## State (2026-06-13)

- Branch `design/switchboard-v2` (unpushed; `main` holds only the v1 baseline; a commit hook blocks direct main commits)
- M0 Plan 1 complete: full `sb` engine, 84 tests green (`.venv/bin/pytest -q`)
- Engine surface: `sb init|seed|claim|file-result|spawn|requeue-stale|query|heartbeat`; exit codes 0 ok / 2 held / 3 nothing-to-claim
- `gate.py`, `rabbit_guard.py` are v1 leftovers — do NOT wire to the new layout; Plans 2/3 replace them
- Hard invariants (each has pinning tests — keep it that way, see PHI-034): write-before-move into claimable lanes; attempts count task failures only, never infra; only a verifier verdict reaches done; every phase ends at a GATE task; seeds all-or-nothing

## M0 remaining work

**Plan 2 — operator surfaces (next, unwritten):**
- `sb brief` (phase review brief from results + AgDRs), `sb stamp` (records feedback on decisions, completes the phase GATE task → unblocks next phase; PR-merge oriented), `sb status --emit` (digest: lanes, stale heartbeats, quota state — the future nexus read-side), notify hook (gate ready / paused_for_human / fleet stalled; channel pluggable, macOS default)
- HDR-010 requirement: pending-review AgDRs route through the digest/notification (tier-2 ping channel)
- Demo-artifact cleanup: `.decisions/` v1 records, `examples/`, DECISIONS.md narrative
- Carry-over note: malformed verifier verdicts leave the verify task to the stale sweep (documented in sb/results.py) — brief/digest should surface these

**Plan 3 — judgment layer (after Plan 2):**
- `/sb-work` skill: claim → dispatch task subagent (tier via tiers.json model override) → file-result loop; quota backoff; heartbeats
- Subagent prompt protocols: task (worktree on context.branch, AgDR-instead-of-prompt with steelman + blast radius), verifier, planner (planning is a task type)
- Tripwire hooks (rabbit_guard v2, deterministic only, no API calls)
- HDR-010 requirements: three-tier escalation in the worker skill; independent fresh-context agent judges AgDR tier assignments (self-assessment is bootstrap-only)
- M0 exit bar: 2-phase toy plan end-to-end with a research-handoff continuation, human stamps the gate
- M0 research tasks: idle-poll tuning (sb claim --wait vs self-pacing); subagent budget enforcement

**Deferred (explicitly):** packaging (schemas are editable-install-only — comment in sb/validate.py), dag.all_edges skipping done/failed lanes, embeddings retrieval, nexus, multi-machine substrate.

## Conventions

- Every engine write passes `sb/validate.py` (jsonschema choke point); new schema fields = version bump
- Decision records: ADR/AgDR (agent), HDR (human), SDR (synthesis) in top-level `decisions/`; append-only `feedback` for amendments
- Tests: pytest, `lay` fixture (tmp dir), `tests/helpers.make_task`; TDD per plan tasks
- Escalation: substance-tiered per HDR-010 — interrupt only on contestable substance; flag-async otherwise; never ceremony prompts
