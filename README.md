# Switchboard — sb engine

A deterministic file-queue engine for multi-agent orchestration. The engine handles
everything mechanical — seeding plans into a task queue, atomic claims with leases,
result routing, mandatory verification, phase gates — as plain files in a git repo.
Judgment (model sessions, the worker skill) and operator surfaces (brief/stamp/
digest/notify) arrive in Plans 2–3. The authoritative design is
`docs/specs/2026-06-12-switchboard-v2-design.md`; target topology is in
`ARCHITECTURE.md`.

## The pipeline

```
plan json ──▶ sb seed ──▶ .switchboard/tasks/queued + GATE per phase (paused)
                              │
                         sb claim (atomic rename + lease)
                              │
              session does the work, writes .switchboard/results/<id>.json
                              │
                         sb file-result ──▶ success: awaiting_verification
                              │                + verify task enqueued
                         verifier claims, files verdict
                              │
                    pass ──▶ done    fail ──▶ requeued with prior attempt
```

## Layout

| Path | Tracked? | What |
|---|---|---|
| `.switchboard/tasks/{queued,active,paused,done,failed}` | gitignored | The lanes — one json file per task |
| `.switchboard/leases/`, `.switchboard/heartbeats/` | gitignored | Claim leases and worker liveness |
| `.switchboard/results/` | gitignored | Sessions drop `<id>.json` here for `file-result` |
| `.switchboard/config.json` | gitignored | Tier/attempt/lease knobs |
| `decisions/` | tracked | Durable decision log (ADR/HDR/SDR records) |
| `plans/` | tracked | Plan jsons (`PLAN-031.json` is the worked example) |
| `schemas/` | tracked | plan / task / result / decision-record contracts |

`tiers.json` still maps tier → model id for the Plan-3 worker skill.

## Quickstart

```bash
pip install -e '.[dev]' && pytest          # the engine is fully unit-tested
sb init --repo .                            # scaffold .switchboard/
sb seed --repo . --plan plans/PLAN-031.json --force
sb claim --repo . --worker-id me            # JSON task on stdout; exit 3 = empty
```

Exit codes are the contract: **0** ok, **2** held/blocked, **3** nothing to claim.
JSON on stdout; skills consume this, humans can too.

## Key invariants

- **Fresh context per task** — each task runs in its own subagent session (Plan 3); context never bleeds.
- **Attempts count task failures only** — infra failures (stale lease, crashed claimer) requeue with attempts unchanged.
- **Write-before-move** — a task body is finalized while un-claimable, then renamed; no write ever follows a move into a claimable lane.
- **Only a verifier verdict reaches done** — success routes to `awaiting_verification` and enqueues a verify task; a `pass` verdict completes it.
- **Every phase ends at a gate** — a paused GATE task gates the next phase, including the last one (the PR-gate invariant).
- **Seeds are all-or-nothing** — the whole plan is validated (schema, forward deps, re-seed clobber) before a single file is written.

## Status / what's not here yet

- `sb brief` / `stamp` / `status` / `notify` — Plan 2. Gates currently advance only
  by hand-editing the GATE task; this is deliberate until the stamp surface lands.
- Worker skill, subagent protocols, tripwire hooks — Plan 3.
- `gate.py` and `rabbit_guard.py` are v1 leftovers kept until Plans 2–3 replace
  them. Do not wire them to the new layout.
