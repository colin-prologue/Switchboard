# AgDR-019: Add Codex as a standalone, non-selectable CLI adapter

**Status:** accepted (2026-07-13)
**Surfaces:** `orchestrator/codex_runner.py`, `CodexConfig`, shared runner
contract tests, and the AI-agnostic agent-pool migration

## Context

Stage 3 made runner selection injectable but intentionally registered only
Claude. The next migration step needs evidence that Codex can satisfy the same
scheduler-facing turn contract before workflow configuration or dispatch can
name it. The local CLI is authenticated through ChatGPT subscription access,
and the current non-interactive interface is `codex exec --json` with
continuation through `codex exec resume <SESSION_ID>`.

This stage must not turn an adapter experiment into a production capability.
It also must not weaken workspace containment merely to imitate the existing
Claude worker's agent-owned git handoff.

## Decision

Add `CodexRunner` and a directly constructible `CodexConfig` without adding a
workflow parser or production selector registration. Launch the configured
command directly, without an intermediate shell, with cwd fixed to the issue
workspace and the prompt on stdin.

The default command applies approval policy `never`, sandbox
`workspace-write`, and workspace-write network access. Fresh runs append
`exec --ignore-user-config --color never --json -`; continuation runs append
`exec resume --ignore-user-config --json <SESSION_ID> -`. `NO_COLOR=1` also
covers resume output. Ignoring user config keeps automation deterministic while
Codex still reads saved authentication from `CODEX_HOME` or the credential
store.

Stage 4 is subscription-only. Remove `CODEX_API_KEY` and `OPENAI_API_KEY` from
the child environment, preserve `CODEX_HOME`, and overlay only `GITHUB_TOKEN`
and `GH_TOKEN` when the orchestrator provides a per-turn GitHub App token.
Credentials are never copied into the workspace.

Normalize JSONL as follows:

- `thread.started` captures the session id and emits `session_started`;
- nonterminal `turn.started` and `item.*` events become bounded notifications;
- `turn.completed` emits `turn_completed` and returns success with usage;
- `turn.failed` and `error` return stable provider-specific failures;
- malformed lines are reported and skipped, while a missing session id or EOF
  without a terminal event fails closed.

Reuse the existing runner invariants for first-output timeout, whole-turn
timeout, external cancellation, stderr isolation, and process-group cleanup.
Do not refactor the proven Claude adapter in this stage.

## Rejected options (steelmanned)

- **Register Codex immediately.** End-to-end scheduler testing would be faster,
  but scheduler budget/stall policy still reads Claude config and the Codex
  sandbox has not proven the existing git handoff. A selectable but incomplete
  provider is more dangerous than an isolated adapter.
- **Use `danger-full-access` so Codex can write `.git`.** This would match the
  current agent-owned commit flow, but removes the workspace containment that
  makes unattended execution acceptable. Stage 5 must solve handoff inside an
  independently isolated environment or move git ownership outside the agent.
- **Use app-server or an SDK.** Those interfaces provide richer control, but
  `codex exec --json` is the documented non-interactive surface, already
  supports saved ChatGPT authentication and resume, and has the smallest new
  dependency footprint.
- **Extract a shared subprocess base from ClaudeRunner.** The adapters share
  lifecycle invariants, but their commands and protocols differ. Refactoring a
  canaried Claude path while introducing Codex would enlarge the regression
  surface without proving more behavior.
- **Allow inline API-key override.** This is convenient for CI, but silently
  changes billing and identity away from the subscription-first decision.
  API-backed operation remains a later explicit mode.

## Blast radius

Additive only. Existing workflow validation still rejects `providers.codex`,
`ClaudeOnlyRunnerSelector` remains the sole production selector, and no shipped
workflow changes. Claude dispatch, budgets, credentials, prompts, and process
behavior are unchanged.

## Weakest point

The safe `workspace-write` sandbox may keep `.git` read-only, so Stage 4 proves
editing, testing, JSONL normalization, and resume but not a ticket-to-PR Codex
handoff. Stage 5 is blocked on an explicit design for git ownership or an
external isolation layer; it must not quietly switch to unrestricted local
execution.
