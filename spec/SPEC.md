# Switchboard Specification

**Owned.** Originally derived from OpenAI Symphony (see `PROVENANCE.md`), now ours
and edited freely. This file is the authority for how Switchboard orchestrates
work. The generic orchestration mechanics we kept close to the original live in
`spec/SPEC.core.md` (the one-time vendored body); the sections below are the parts
we own and have re-bound for **Claude** (execution) and **GitHub** (tracker).

Generate the orchestrator (Phase 1) by pointing Claude Code at this file **and**
`spec/SPEC.core.md` together.

## 0. How this spec is assembled

- `spec/SPEC.core.md` — paste the vendored Symphony orchestration spec here, once.
  It defines the state machine, polling/reconcile/retry, workspace lifecycle and
  safety invariants, Liquid prompt rendering, and observability. We keep these as
  written unless a section below overrides them.
- `spec/SPEC.md` (this file) — the owned overrides and bindings. Where this file
  and the core disagree, **this file wins.**

---

## 1. Execution binding — coding agent → Claude

The core's agent-runner contract is normative on **message ordering and logical
fields** (session id, completion state, approval handling, token/usage telemetry),
not exact JSON names. We implement that over the Claude CLI.

| Core / Codex concept                | Claude binding                                                                 |
|-------------------------------------|--------------------------------------------------------------------------------|
| agent `command` (`codex app-server`)| `claude -p --output-format stream-json` (subprocess, line-delimited JSON)       |
| thread/turn start                   | first `claude -p` invocation; capture `session_id` from the `system/init` event |
| continuation turn (reuse thread)    | `claude -p --resume <session_id>` (same workspace)                              |
| turn completed / failed             | terminal `result` message; map `result.subtype` → Succeeded / Failed            |
| `max_turns`                         | `--max-turns`                                                                   |
| approval / auto-approve             | non-interactive permission mode + `--allowedTools`; unresolved permission request → fail the attempt (core's "user-input-required = hard failure") |
| sandbox / safety invariants         | **PreToolUse hooks** vetoing tool calls outside the per-issue workspace (stronger than advisory sandbox) |
| token accounting                    | `result` message `usage` + `total_cost_usd`; add `--max-budget-usd` as a hard per-run cost ceiling |
| client-side tracker tool            | the agent's `gh` tooling; the token never enters the workspace                  |

The front-matter execution block is named **`claude:`** (pass-through, same role as
the core's codex block): `command`, `max_turns`, `max_budget_usd`, `turn_timeout_ms`,
`read_timeout_ms`, `stall_timeout_ms`.

**Win over the Codex path:** `--max-budget-usd` gives an always-on orchestrator a
hard per-run cost stop the original lacks. Budget is enforced at two layers:
the flag caps each `claude -p` invocation, and the worker additionally tracks
the cumulative cost across a session's turns — when the sum reaches
`max_budget_usd` the worker ends the session normally instead of starting
another turn (the session cap in §4 then bounds total re-dispatch spend).

---

## 2. Tracker binding — Linear → GitHub Issues

Three required adapter operations (fetch candidates / fetch by states / fetch
states by ids) implemented against GitHub's GraphQL API (or `gh api graphql`).
Normalized outputs must match the core's issue domain model.

| Core / Linear concept               | GitHub binding                                                                 |
|-------------------------------------|--------------------------------------------------------------------------------|
| `tracker.kind: linear`              | `tracker.kind: github`                                                          |
| `project_slug` (Linear slug)        | `tracker.repo: owner/name` (one process = one repo)                            |
| `api_key` / `LINEAR_API_KEY`        | `GITHUB_TOKEN` (GitHub App installation token preferred)                        |
| workflow **state** (first-class)    | **status label** convention `status:<name>` (GitHub issues are only open/closed)|
| `active_states`                     | `["triage", "todo", "in progress"]` → labels `status:triage`, `status:todo`, `status:in-progress` |
| terminal states                     | issue **closed** → terminal; `status:*` gate labels are non-active              |
| `blocked_by` (Linear `blocks`)      | GitHub **native issue dependencies** (blocked-by) + sub-issues, read via GraphQL|
| `issue.identifier`                  | the issue **number** (workspace root is per-project, so numbers don't collide)  |
| tracker **writes**                  | done by the **agent** via `gh` (move label, comment, link PR), not the orchestrator |

**State mapping is the one real semantic gap.** Model state as `status:*` labels:
the adapter normalizes a `status:todo` label into `state: "todo"`. Gate states
(`status:drafting`, `status:plan-review`, `status:human-review`) are **not** in
`active_states`, so the orchestrator never dispatches a gated ticket and parks at a
handoff state — the human gate is enforced by state, costing zero orchestrator
code. See `methodology/METHODOLOGY.md`.

**EMU note:** native issue dependencies are recent; if cross-repo blocked-by is
restricted in your org, fall back to sub-issues or a `status:blocked` label.

---

## 3. What we kept from the core (do not re-bind)

Orchestration state machine, polling/reconcile/retry/backoff, workspace lifecycle
and the three safety invariants, Liquid prompt rendering, and observability —
generate these straight from `spec/SPEC.core.md`. The deliberate empty slot is
workspace **population**, which we fill with `hooks/after_create.sh` +
`hooks/before_run.sh` (clean clone + per-issue branch).

## 4. Owned extensions (beyond the core)

These are ours, layered on top, not in the original Symphony spec:

- **Methodology as config** — gate-states and the IDSD layer split, carried in
  `WORKFLOW.md` + `methodology/METHODOLOGY.md`.
- **Convention root** — a per-project prefix (default repo root; `self/` for
  dogfooding) under which a project's `.switchboard/intents/` and `.decisions/`
  live, so the orchestrator can manage its own repo as a project without polluting
  the general-purpose root.
- **Decision-corpus MCP** (later phase) — a tool the agent queries before
  architecture decisions and writes ADRs into; the cross-task memory that keeps
  parallel agents convergent.
- **Role-pinned worker sessions** — core §16.5 keeps a session's turn loop
  running while the issue is in *any* active state. Switchboard renders the
  turn-1 prompt from dispatch-time state (the `status:triage` branch swaps the
  agent's role), so the worker instead breaks the loop as soon as the refreshed
  state differs from the state it was dispatched under — including
  active → active transitions (triage PASS: `status:triage → status:todo`).
  The normal continuation re-dispatch then starts a fresh session in the new
  role instead of feeding continuation prompts to a stale one. Consequence: an
  agent cannot relabel its own issue mid-session and keep working — a state
  transition is always a session handoff.
- **Session cap + parking** (`agent.max_sessions_per_issue`, default 3) — the
  core's continuation loop re-dispatches an active issue indefinitely; with a
  paid execution adapter that is an unbounded-spend path. After N worker
  sessions on one issue in a process lifetime, the orchestrator *parks* it:
  claim released, workspace and logs preserved, one notification comment posted
  on the issue, and no re-dispatch until the issue's `updated_at` changes
  (i.e., a human touched it). Caps are diagnostic checkpoints, not kill
  switches. The parking comment is the single deliberate exception to the core
  §11.5 orchestrator-never-writes-the-tracker boundary; nothing else is alive
  to notify the human at that point.
