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
| approval / auto-approve             | non-interactive permission mode + `--allowedTools`; a denial is surfaced to the agent (not an attempt-killer) — a session that cannot finish because of one ends in a non-success `result`, which fails the attempt. Never blocks on user input (core §10.5). Ratified 2026-07-03 (AgDR-004 addendum): this soft semantic is what shipped and was validated (PRs #13/#17). |
| sandbox / safety invariants         | **PreToolUse hooks** vetoing tool calls outside the per-issue workspace (stronger than advisory sandbox) |
| cost accounting                     | `result` message `total_cost_usd` drives budget enforcement; `--max-budget-usd` as a hard per-run cost ceiling |
| client-side tracker tool            | the agent's `gh` tooling; the token is never **written** into the workspace (clone auth via `gh auth setup-git`). The agent process does inherit the orchestrator's environment — Bash-level access to `$GITHUB_TOKEN` is inside the documented v1 guard scope (AgDR-004). |

The provider-instance form of the front-matter execution block is
**`providers.claude`**, with `kind: claude-cli` plus the pass-through fields
`command`, `max_turns`, `max_budget_usd`, `turn_timeout_ms`, `read_timeout_ms`,
and `stall_timeout_ms`:

```yaml
providers:
  claude:
    kind: claude-cli
    command: "claude -p --verbose --output-format stream-json"
    max_turns: 100
    max_budget_usd: 5
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
```

The top-level **`claude:`** block remains a supported legacy form with the same
fields and defaults. During the dual-read migration (AgDR-017), either form may
be used. If both are present, they must resolve to equal typed `ClaudeConfig`
values; otherwise startup/reload fails with `conflicting_provider_config`
instead of choosing one silently. A provider envelope must contain the canonical
`claude` id with `kind: claude-cli`. Until another adapter is implemented, other
provider ids and kinds fail validation rather than being ignored. The shipped
workflow template remains on the legacy form to continuously exercise backward
compatibility. Stage 2 introduced no provider selection; the Claude-only Stage
3 boundary is specified below.
Provider-enveloped Claude settings are strict: unknown fields, malformed field
types, and boolean `max_budget_usd` values fail parsing instead of falling back
to defaults. Legacy coercions remain unchanged for compatibility. The workflow
loader also rejects textual duplicate YAML mapping keys; a duplicate must never
erase an earlier `claude` or `providers` value before dual-form conflict
detection runs. Standard YAML merge-key inheritance and explicit overrides
remain supported.

At dispatch, the scheduler selects one `AgentRunner` through an injected
`AgentRunnerSelector(Config, Issue)` boundary (AgDR-018). The production
selector remains Claude-only and returns `ClaudeRunner(cfg.claude())` for both
configuration forms. Selection happens before claim or tracker mutation, once
per worker session; all continuation turns in that session use the same runner
instance. The selected provider id is retained on the running entry and emitted
in worker lifecycle logs. Unsupported provider ids remain startup/reload errors;
this boundary does not enable Codex, pooling, fallback, or issue overrides.

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
| `blocked_by` (Linear `blocks`)      | GitHub **native issue dependencies** (blocked-by), read via GraphQL (`blockedBy` connection) |
| `issue.identifier`                  | the issue **number** (workspace root is per-project, so numbers don't collide)  |
| tracker **writes**                  | done by the **agent** via `gh` (move label, comment, link PR), not the orchestrator |

**State mapping is the one real semantic gap.** Model state as `status:*` labels:
the adapter normalizes a `status:todo` label into `state: "todo"`. Gate states
(`status:drafting`, `status:plan-review`, `status:human-review`) are **not** in
`active_states`, so the orchestrator never dispatches a gated ticket and parks at a
handoff state — the human gate is enforced by state, costing zero orchestrator
code. See `methodology/METHODOLOGY.md`.

**EMU note:** native issue dependencies are recent; if cross-repo blocked-by is
restricted in your org, fall back to a `status:blocked` label (non-active, so
it gates dispatch by state — no adapter support required).

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
  transition is always a session handoff. Forward constraint for any
  orchestrator-applied dispatch label (e.g. issue #14's `status:in-progress`
  visibility marker): the label must be applied **before** the worker captures
  dispatch-time state, or the captured state must be defined as the post-label
  value — otherwise every session self-terminates after turn 1. Adjust the
  labeling mechanism, not the role-pin rule (AgDR-005).
- **Session cap + parking** (`agent.max_sessions_per_issue`, default 3) — the
  core's continuation loop re-dispatches an active issue indefinitely; with a
  paid execution adapter that is an unbounded-spend path. After N worker
  sessions on one issue in a process lifetime, the orchestrator *parks* it:
  claim released, workspace preserved (plus the `after_run` run log beside it),
  one notification comment posted on the issue, and the durable `status:parked`
  label applied. A parked issue is not re-dispatched while it carries that
  label; because the label lives in the tracker, the park decision survives a
  process restart. **Unpark is deliberate: a human removes the `status:parked`
  label** (e.g. moves the card off *Parked* on the board), which also resets the
  per-issue session counter. A stray edit or comment no longer unparks — this is
  what structurally forecloses the OBS-022 self-unpark loop (the park decision
  never reads `updated_at`). Caps are diagnostic checkpoints, not kill switches.
  The comment and the label are the two deliberate exceptions to the core §11.5
  orchestrator-never-writes-the-tracker boundary; nothing else is alive to
  notify the human at that point. The in-memory `parked` set survives only as
  session-counter bookkeeping for within-run unparks; it is not load-bearing for
  the park decision (AgDR-008 supersedes AgDR-002's in-memory-park weakness).
