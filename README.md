# Agent Orchestrator (→ "Switchboard")

> **Read `ARCHITECTURE.md` first.** This bundle is the **flat reference implementation** —
> everything in one repo so the demo runs end to end. The target topology (a shared installed
> engine + a per-project `.switchboard/` instance + a top-level `decisions/` log) and the
> deferred nexus are described there. Founding decisions are recorded in `decisions/`.

A lightweight, spec-driven orchestration framework for planning and running coding work
across model tiers, with decisions captured as durable institutional memory and human
oversight at phase gates. Coordination is plain files in a git repo — no services to stand
up. Built to be loose: every contract is a versioned schema you can evolve independently.

## The pipeline

```
goal ──▶ bootstrap.py ──▶ plan + decomposition decision ──▶ seeds the queue
                │                                                │
          pulls precedent                                   workers (tier-pinned)
          from the log                                      claim → run in a FRESH
                                                            session → write result +
                                                            decisions → repeat
                                                                │
        human gate (gate.py) ◀── phase complete ◀──────────────┘
          review brief → approve/revise → unblocks next phase
                │
          feedback lands in the decision log ──▶ next goal grounds in it
```

Each model session is fresh per task, so context never bleeds between tasks. Single-session
spirals are caught by `rabbit_guard.py`. Cross-task pathologies mostly can't occur — workers
are isolated and coordinate only through the shared files.

## Layout

```
bootstrap.py            Front door: goal → plan → seeded queue.
worker.py               Tier-pinned worker loop (run several, one per tier).
gate.py                 Human review gate: status / brief / stamp.
query_decisions.py      Zero-token precedent retrieval over the decision log.
rabbit_guard.py         Single-session guard (Claude Code PostToolUse + Stop hook).
tiers.json              The one place tiers map to model ids.
schemas/                plan / task / decision-record contracts (JSON Schema).
hooks/settings.example.json   Where to register rabbit_guard with Claude Code.
plans/                  Plans live here (PLAN-031.json is the worked example).
.decisions/             The decision log (ADR/HDR/SDR records + DECISIONS.md index).
.tasks/{queued,active,paused,done,failed}   The git-coordinated work queue (lanes).
reviews/                Generated review briefs.
examples/               Reference artifacts: a seeded task, a rendered review brief.
```

The lanes, `reviews/`, and `.results/` ship empty (just `.gitkeep`); the tools fill them.

## Quickstart — run the demo with no model wired

```bash
git init && git add -A && git commit -m init      # tools coordinate via git

# Seed the example plan. It has a BLOCKING question, so the front door holds for sign-off:
python3 bootstrap.py --plan plans/PLAN-031.json --repo .

# Acknowledge it and seed for real:
python3 bootstrap.py --plan plans/PLAN-031.json --repo . --force
#   → 4 tasks: 3 opus, 1 haiku. 1 ready now, 3 waiting on dependencies.
#   → GATE placeholders dropped into .tasks/paused for the two human gates.

# A worker finds the one ready task (dry run pauses it, since no model is wired):
python3 worker.py --tier opus --repo . --once

# Inspect / review a gate:
python3 gate.py status --plan PLAN-031 --repo .
python3 gate.py brief  --plan PLAN-031 --phase PH-1 --repo .

# Approve it — completes the gate task, which unblocks the next phase:
python3 gate.py stamp  --plan PLAN-031 --phase PH-1 --action approve \
        --note "Agree. Revisit if we adopt thread-local caching." --reviewer colin --repo .
```

`status` shows whether a gate is ready; `brief` renders the scannable summary (see
`examples/review-brief-PLAN-031-PH-1.md`); `stamp` records your verdict as feedback on the
phase's decisions plus a human decision record, and on approval lets the next phase run.

## Wiring a real model

Both the planner and the workers run model sessions through an `--executor` shell template.
The session does the work and **writes structured files** (the worker never parses freeform
output):

- Planner writes `plans/<id>.json` and a decomposition decision to `.decisions/`.
- Worker session writes `.results/<id>.json` (matching `schemas/task.schema.json` →
  `result`) and any decision records to `.decisions/`.

```bash
# {prompt_file} and {model}/{tier} are substituted in:
python3 bootstrap.py --goal "Add a session cache that holds up under load" \
        --planner-tier fable --repo . \
        --executor 'claude -p --model {model} < {prompt_file}'

python3 worker.py --tier opus --repo . \
        --executor 'claude -p --model claude-opus-4-8 < {prompt_file}'
```

Point the template at whatever invokes a model session pinned to that tier (Claude Code,
an SDK script, etc.). Map tiers to current model ids in `tiers.json`.

### rabbit_guard hook

Merge `hooks/settings.example.json` into `.claude/settings.json` and set an absolute path to
`rabbit_guard.py`. It needs `ANTHROPIC_API_KEY` for the fresh-reviewer call; with no key it
fails open. See the top of `rabbit_guard.py` for the env knobs.

## Notes / seams

- **Git is the lock.** Fine for a modest pool; two workers racing a task resolve via a
  rejected push + rebase. For heavy parallelism, swap in a real lock service — nothing else
  changes.
- **Versioned contracts.** Schemas use `additionalProperties: false`, so a new field is a
  deliberate version bump, not a silent edit. Loose "expansion-joint" fields (phase, level,
  tags, verify.kind, tier) are open strings so the model can evolve without a break.
- **`effort`** on Fable is wired in `rabbit_guard.py` but auto-dropped on a 400 — confirm the
  current API field before relying on it.
- **Deferred:** the rich visual decision view. The decision records are normalized for it;
  for now `DECISIONS.md` is the scannable index.
```
