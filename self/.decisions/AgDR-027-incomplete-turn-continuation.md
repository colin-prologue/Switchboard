# AgDR-027: `incomplete` is a fourth neutral turn outcome; Codex context exhaustion stays a failure

**Status:** accepted (2026-07-27, issue #47)
**Surfaces:** `TurnResult` shared contract, the scheduler turn loop
(`scheduler.py`), Claude/Codex runner adapters, `FailureClass` taxonomy,
failure classification, provider circuit trigger set, and worker
cancellation / #61 handoff boundary.

## Context

A worker turn can end early and benignly: the provider hit a resource ceiling,
not a failure. The turn loop collapsed every non-`succeeded` status into a
`WorkerFailure` and re-dispatched a fresh session with no continuation. Observed
live on #14: a Claude task needing more than `--max-turns` re-derived context
from scratch each attempt and was structurally uncompletable regardless of
retries, even though the orchestrator's resume-by-session machinery exists for
exactly this shape of work — it only ran on success.

The two engines both stop early but with different recovery properties. Claude
exhausts `--max-turns`, emits result subtype `error_max_turns`, and its session
is intact — `--resume` recovers. Codex exhausts the model context window, emits
`turn.failed` whose `error.message` says it "ran out of room in the model's
context window," and the conversation *is* the problem — resuming reproduces the
wall, and a fresh session re-running the task prompt is the #14 loop. Codex
`turn.failed` carries only `error.message` (no `code`), so any detection must
match on text.

## Decision

Add one neutral outcome and one neutral, runner-owned continuation decision — no
new module:

- `TurnResult.status` gains `"incomplete"`: the turn ended early and benignly;
  work is unfinished; **not** a failure.
- `TurnResult.continuation: Continuation | None`, a small enum owned by the
  runner (only the adapter knows whether its session survives). Sole member:
  `RESUME_SESSION`.
- `FailureClass` gains `PROVIDER_CONTEXT_EXHAUSTED` for the Codex case.

Claude `error_max_turns` **with a session id** → `incomplete` + `RESUME_SESSION`;
the scheduler continues the SAME session via `--resume` with the continuation
prompt (not the task prompt), counting one orchestrator turn and leaving
`turn_succeeded` False (still active, cancellable, not a terminal handoff). All
other bounds (`agent.max_turns`, cumulative budget, role-pin, required labels)
still apply to continuation turns. `error_max_turns` **without** a session id
stays a failure.

Codex context-exhaustion text → `status="failed"`,
`failure_class=PROVIDER_CONTEXT_EXHAUSTED`, matched in `_TEXT_PATTERNS` (not
`_CODEX_CODES`). It stays a failure with today's retry/backoff semantics and does
not open the provider circuit (it is task-shaped, not provider-health-shaped).

## Rejected alternatives (steelmanned)

- **Give Codex `Continuation.NEW_SESSION_SAME_TASK`.** Symmetric with Claude and
  keeps both providers "recoverable." But a fresh session re-running the same
  prompt is precisely the #14 loop with retry accounting disabled — strictly
  worse than failing honestly. No headless Codex recovery exists today
  (openai/codex #19842, #16033); inventing one hides the condition instead of
  naming it. Deferred to follow-up work gated on OpenAI shipping an exec-mode
  compact affordance.
- **Special-case `error_max_turns` at the scheduler.** Re-couples the neutral
  scheduler to a Claude result subtype and undoes the Stage 1–7 provider
  separation. The neutral concept is "benign early stop; recovery is
  provider-owned," expressed once in `TurnResult`.

## Blast radius

Every consumer of a turn result: the turn loop (new CONTINUE branch), the
`turn_succeeded`/cancellation flag, session-id freshness on resume, the
`agent.max_turns` and budget bounds, failure/backoff/session accounting (a
continuation burns neither), and the provider circuit (neither `incomplete` nor
`PROVIDER_CONTEXT_EXHAUSTED` triggers it).

## Weakest point

`_TEXT_PATTERNS` is shared by both classifiers, so the Codex context-exhaustion
regex is reachable by Claude detail text too. It is intentionally anchored on the
Codex-specific phrase "ran out of room in the model's context window," which
Claude never emits; if the real Codex wording drifts, the class silently falls
back to `WORKER_FAILURE` — the pre-#47 status quo, diagnosable but undifferentiated.
