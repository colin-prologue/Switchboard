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
- AgDRs (all director-reviewed 2026-06-19, **approved**): ADR-001 (guard logic in tested `hooks/` package, not `sb/`); ADR-002 (`.switchboard/` upward discovery, approved with a containment cap on the walk, `_MAX_UP=16`); ADR-003 (guard arms on 2nd trip OR exhausted nudge budget — err toward stopping); ADR-004 (intervention-learning loop — shape confirmed = reuse decision-grounding; build deferred to the oversight layer)
- [docs/ROADMAP.md](docs/ROADMAP.md) — milestone status + the Plan 3 A/B/C/D decomposition, runway, and deferred work (the planning detail; keep it here, not in this file)

## Resuming a session (handoff protocol)

This repo **is** the handoff — sessions are disposable; start a fresh one per merged sub-plan (the merge is a clean phase boundary that avoids context rot). No transcript is needed to pick back up.

- **To resume cold:** read in order → the State block below → [docs/ROADMAP.md](docs/ROADMAP.md) → any `pending-review`/`proposed` records in [decisions/](decisions/) → the active plan's `- [ ]` checkboxes + `git log`. The State block's **"Next:"** line is the single starting action.
- **Before `/clear` or switching sessions (the handoff):** (1) commit + push all work — nothing may live only in the worktree or transcript; (2) update the State block (what's done + the one "Next:" action); (3) flush loose context — a settled decision → an ADR in `decisions/`, an open question → a ROADMAP note. Optionally run `/oracle-preclear` first to capture any cross-project philosophy before clearing.
- **Litmus test:** could a brand-new session start the "Next:" action from these files alone, with zero reference to the prior conversation? If not, something is still transcript-only — capture it.

## State (2026-06-22)

- Branch `design/switchboard-v2` is the integration/dev trunk (GitHub default branch); **`main` now holds the v2 checkpoint** — PR #2 merged engine + operator surfaces + worker loop + safety guards into it (2026-06-20, merge commit `2b8c052`), incl. the Codex C1/C2/C3 remediation. M0 still in progress; next checkpoints continue design → main. A commit hook blocks direct main commits
- M0 Plan 1 + Plan 2 complete (engine core; operator surfaces). Plan 3 decomposed into A/B/C/D (see [ROADMAP](docs/ROADMAP.md)); HDR-012 recorded (deliberation is a separate front-end layer, coupled only via the plan/goal artifact)
- **M0 Plan 3-A IMPLEMENTED** (140 tests at landing): `/sb-work` worker-loop skill + task/verifier prompt protocols (`.claude/skills/sb-work/`), `sb release` (infra-requeue, attempts unchanged), `sb block` (synthesized blocked result → paused-for-human when a subagent returns no result file; the B deny→blocked contract), `sb/loopledger.py` (token-free ledger + productive/churn diagnostic). Skill owns all git; engine stays git-free. Prompt protocols are reviewed-not-tested by design (exercised live in D)
- **M0 Plan 3-B IMPLEMENTED** (2026-06-18, 171 tests green): token-free safety layer — `hooks/sb_guard.py` (deterministic tripwire guard, per-subagent state by `agent_id`, two-strike nudge→deny), `hooks/sb_quota.py` (rate-limit detector → `quota.json`, advisory only), `hooks/sb_monitor.py` (launchd/cron token-free liveness/quota/notify + early-churn detector reading A's loop-ledger). v1 `rabbit_guard.py` deleted (paid reviewer gone; verification is A's lane). No engine verbs added; logic lives in the tested `hooks/` package (ADR-001). NOT yet built: HDR-010 escalation routing (C); per-task budget wiring (deferred)
- **M0 A-continuation IMPLEMENTED + MERGED** (2026-06-21, 182 tests, PR #3 merged to trunk, merge `46316cd`): research-handoff chain. Engine `spawn_research` was already built; this added the `paused_for_research` result outcome + `research` block (result schema 0.2.0), `file-result`→`spawn_research` delegation (ADR-005), the `sb result` read verb feeding research findings to the continuation (ADR-006), prompts, a full-chain integration test, and the Codex C1 fix (`_consume_partial` on the chain-depth-cap path)
- **M0 A-planner IMPLEMENTED** (2026-06-22, 192 tests, branch `plan/sb-a-planner` — **awaiting gate/merge**): `sb seed --goal "<goal>"` allocates the next `PLAN-NNN` and enqueues ONE planner task (`PLAN-NNN/PH-0/T-1`, `done.verify.kind == "plan"`; ADR-007, pending-review). Loop routes that discriminator → new `planner-protocol.md` (planner emits `plans/<id>.json` + an SDR, sets `decision_ref`). Verification reuses the **standard verifier path** (verifier subagent schema-validates the committed plan in the branch worktree; verifier-protocol gained a `kind==plan` branch) — no engine verification branch, no schema change. `seed --goal` enqueues exactly one bootstrap task (no gate); the gate invariant governs the seeded plan it PRODUCES, which a human expands with the existing `sb seed --plan`. Delivered: `seed.seed_goal`/`allocate_plan_id`, `--goal`/`--plan` CLI (mutually exclusive, `--tier` default opus), SKILL routing branch, `planner-protocol.md` + verifier `kind==plan` (both reviewed-not-tested, live in D)
- **Next: C** (HDR-010 escalation + ADR-004 learning loop) — once A-planner clears the gate. Then the D exit bar (see ROADMAP). (Open seam carried into D: planner PROMPT is reviewed-not-tested; exercised live there.)
- AgDRs: ADR-001/002/003/004 (PR #2 gate, 2026-06-19), ADR-005/006 (PR #3 merge gate, 2026-06-21) all approved. **ADR-007 (A-planner task representation) is pending-review at the `plan/sb-a-planner` gate.**
- Engine surface (Plan 1+2+3-A + A-continuation + A-planner): `sb init|seed|claim|file-result|result|release|block|spawn|requeue-stale|query|heartbeat|status|brief|stamp|notify`; exit codes 0 ok / 2 held / 3 nothing-to-claim (also: `sb result <id>` exits 3 = no result yet). `sb seed` now takes `--plan <path>` (expand a plan) XOR `--goal "<goal>"` (enqueue one planner task; `--tier` default opus). B adds no verbs. A-continuation adds `result` (read verb) + the `paused_for_research` result outcome (engine spawns research + re-enqueues the parent as a continuation)
- Hard invariants (each has pinning tests — keep it that way, see PHI-034): write-before-move into claimable lanes; attempts count task failures only, never infra (`sb release` and stale-requeue preserve attempts); only a verifier verdict reaches done; every phase ends at a GATE task; seeds all-or-nothing; `sb stamp` completes the GATE (paused→done) — the only thing that unblocks the next phase; quota is advisory, never gates a claim (HDR-011); the digest carries pending-review AgDRs (HDR-010 tier-2 channel)

## Conventions

- Every engine write passes `sb/validate.py` (jsonschema choke point); new schema fields = version bump
- Decision records: ADR/AgDR (agent), HDR (human), SDR (synthesis) in top-level `decisions/`; append-only `feedback` for amendments
- Tests: pytest, `lay` fixture (tmp dir), `tests/helpers.make_task`; TDD per plan tasks
- Escalation: substance-tiered per HDR-010 — interrupt only on contestable substance; flag-async otherwise; never ceremony prompts
