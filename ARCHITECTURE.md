# Switchboard — Architecture (target)

This bundle currently ships as a **flat reference implementation** (everything in one repo
so the demo runs). This document is the **target topology** we are restructuring toward.
The decisions behind it are recorded in `decisions/` (HDR-001…005).

## Three concerns, kept separate

1. **The engine** — `bootstrap.py`, `worker.py`, `gate.py`, `query_decisions.py`,
   `rabbit_guard.py`, `schemas/`, `tiers.json`. Generic, identical across projects.
   Installed once, **versioned, never copied** (a pip CLI or a Claude Code plugin exposing
   the commands).
2. **The per-project instance** — a `.switchboard/` folder in each project: the queue lanes,
   in-flight plans, results, and a `config.json` (project id, tier overrides, executor
   command, pinned engine version). Operational/transient — gitignore-able.
3. **The durable decision log** — a top-level `decisions/` folder in the project repo. This
   is documentation of the code and must be browsable by anyone in the repo. It travels with
   the code through history. (See HDR-003.)

The **work product** — the project's own source — is edited by workers via each task's
`repo_state`; orchestration scaffolding stays out of it.

## Deployment — the plugin pattern (HDR-002)

- Install the engine once. `switchboard init` scaffolds `.switchboard/` + `config.json` into
  a repo (and a top-level `decisions/` if absent).
- **Pin the engine version per project** in `config.json`, so a framework update can't
  silently change a regulated project's behavior — you opt in by bumping the pin.
- The **worker fleet can be central**: one pool of tier-pinned processes, each pointed at a
  project's state via `--repo`, with **scoped access to only that project's source**.
  Central execution, decentralized memory, per-project isolation — all at once.

This is the same shape as a Claude Code plugin: shared logic installed centrally,
per-project state held locally.

## Coordination model (HDR-001)

Tier-pinned, ephemeral workers that claim a task, run it in a **fresh** model session, write
a result + any decisions, and move on. No persistent peers, no live peer-to-peer messaging.
Cross-task pathologies mostly can't occur; single-session spirals are caught by
`rabbit_guard.py`.

## Deferred: the nexus (HDR-005)

A control plane **above** N switchboards. It **aggregates, it does not own** — the only
durable state it holds is a **registry** of which projects exist and where. It reads each
switchboard's emitted **status digest** and renders one cross-project **"what needs me"**
queue (gates awaiting sign-off, blocking questions, failures); it **dispatches operator
commands** (update goal, stamp gate, leave feedback) down to the right project. Delivery is a
cockpit (CLI/web) and/or a Teams channel. **Parked** until the single-project Switchboard is
stood up.

## Restructure tasks (the work to start now)

1. Split the engine from the instance; package the engine; add `switchboard init`.
2. Move per-project state into `.switchboard/`; repoint the tools from the demo's
   `.decisions/` to the project's top-level `decisions/`.
3. Add `config.json` with the pinned engine version, tier overrides, and executor.
4. Add a `switchboard status --emit` digest (also the read-side primitive the future nexus
   consumes).
5. Rename the bundle `agent-orchestrator` → `switchboard`.
