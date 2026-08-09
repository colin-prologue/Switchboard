# AgDR-032: the terminal record's `is_error` decides the outcome; its text decides only the class

**Status:** proposed (2026-08-08, issue #116)
**Surfaces:** `runner.py` result handling (SPEC.md §1 subtype mapping),
`TurnResult.status/error/failure_class` consumers (ProviderCircuit, session
accounting, park comments), `tests/fake_claude.py` fidelity

## Context

Ground truth (`orchestrator/tests/fixtures/claude_cli_auth_logged_out.jsonl`,
claude-code `2.1.226`, PR #124): a logged-out run's terminal record carries
`subtype: "success"` **with** `is_error: true`, `terminal_reason: "api_error"`,
and `result: "Not logged in · Please run /login"`. The typed
`"error": "authentication_failed"` value sits on the ASSISTANT record, not the
terminal one.

`runner.py` branched on `subtype` alone, so a logged-out CLI returned
`status="succeeded"` and classification was never reached. This is worse than a
misclassification: the worker exits with no exception, so `_on_worker_done`
calls `record_success()` — a logged-out CLI actively RESET the provider circuit
while spending one session per no-op turn until the issue false-parked at the
session cap. #112/#114 are the live evidence: parked at the cap, zero diffs, no
failure logs, no backoff.

AgDR-025/026 settled the failure taxonomy and made `failure_class` the circuit's
only input; AgDR-027 settled `incomplete`. None of them settled *which field of
the terminal record decides the outcome*. That is the ambiguity this closes.

## Decision

**Two independent steps, never conflated.**

1. **Outcome gate (text-independent).** A result that would otherwise take the
   `subtype == "success"` branch but carries `is_error: true` yields
   `status="failed"`, with `error = terminal_reason or subtype or "failed"`
   (never `None`, which `WorkerFailure(result.error or …)` depends on). The gate
   is ordered AFTER the `error_max_turns`-with-session-id branch: a benign early
   stop stays `incomplete` + `RESUME_SESSION` whatever `is_error` says (whether
   the real CLI sets it there is unknowable from the one capture — the ordering
   makes the question moot). An ABSENT `is_error` is false, so a CLI version
   that drops the field cannot fail every turn.
2. **Class derivation.** `failure_class` comes from `classify_claude_failure`
   over a NEW extractor used only by the gate branch, with precedence
   `detail = result or terminal_reason` (`result` is the richer CLI-authored
   text; `terminal_reason` is the fallback). `_structured_error_text` and the
   pre-existing else-branch are untouched — the terminal record has no top-level
   `error` key, so the old extractor returns `""` there. `_CLAUDE_CODES` needs no
   entry: `api_error` misses the map and the lowercased result text hits the
   existing `\bnot logged in\b` pattern.

**Trust boundary.** The gate IS the boundary. On a gated record, `result` is
CLI-authored error text (fixture evidence) and IS classification input. The old
invariant "model result text cannot create a provider failure class" is
NARROWED, not abandoned, to: *on any result the gate does not claim*. A gated
record whose text matches a different pattern (rate-limit phrasing) classifies to
that class **by design**, pinned by its own test.

## Rejected options

- **Blanket `is_error ⇒ PROVIDER_AUTHENTICATION`.** Steelman: the only capture we
  have is an auth failure, and it latches the circuit exactly when we want it
  latched. Rejected because `PROVIDER_AUTHENTICATION` is a LATCHED class: a
  transient API 5xx — which also sets `is_error` — would latch the provider until
  an operator intervenes. That is a worse outage than the bug being fixed.
  Unrecognized gated errors fall to `WORKER_FAILURE` and retry with backoff.
- **Classify from the assistant record's `"error": "authentication_failed"`.**
  Steelman: it is the typed signal, and mapping it into `_CLAUDE_CODES` needs no
  new extractor. Rejected because it makes model-adjacent stream content a
  circuit input: any assistant message could then latch the provider. The
  terminal record's own fields are the only trustworthy source, and the negative
  test pins that a run merely quoting "not logged in" in prose stays a success.
- **Widen `_structured_error_text` to also read `result`/`terminal_reason`.**
  Steelman: one extractor, fewer moving parts. Rejected because it silently
  changes the pre-existing else-branch for every non-success subtype — including
  `error_max_turns_no_session`, whose `result` text is model prose. Blast radius
  is contained by giving the gate its own extractor.
- **Put the gate before the `error_max_turns` branch.** Steelman: "an error is an
  error", one rule. Rejected: it would convert a resumable early stop into a
  failed turn if the real CLI sets `is_error` there, losing the session AgDR-027
  exists to preserve. The fake asserts the worst case (`is_error: true`, marked
  UNVERIFIED) precisely so the ordering regression bites.

## Blast radius

Claude runner only; codex untouched (#109/PR #113 own that side). No change to
`_CLAUDE_CODES`, `_TEXT_PATTERNS`, `FailureClass` membership, circuit policy
(AgDR-026), or the scheduler. Turns that previously returned a false `succeeded`
now fail — for logged-out runs that means refund instead of spend, latch instead
of reset, and a failure-shaped park comment instead of a cap-shaped one. Any
other CLI condition that sets `is_error` on a success-subtype record also flips
from silent success to `WORKER_FAILURE`, which is the intended direction: a turn
the CLI itself marks failed was never a success.

## Weakest point

`is_error` is load-bearing on a field we have exactly ONE capture of. If some
claude-code version sets `is_error: true` on a record that genuinely did the
work (a cleanup warning, a post-turn telemetry error), every such turn now fails
and retries — burning budget on work that already landed. The absent-field
default (false) limits the blast to versions that set it *wrongly*, not versions
that drop it, and the fake's UNVERIFIED markers keep the assumption visible; but
the only honest re-check is a refreshed capture, not reasoning about the CLI.
