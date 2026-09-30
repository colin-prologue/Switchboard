# AgDR-2026-09-30 - Sonnet 5.5 model pin and artifact-based Codex handoff

**Status:** accepted
**Date:** 2026-09-30
**Scope:** shared Claude provider commands in the base, prototype, committed project, and Codex-pilot workflows.

## Decision

Switchboard pins its Claude CLI execution commands to `claude-sonnet-5-5`.
The update applies to both normal selectable templates and their committed
project snapshots, including the isolated Codex pilot workflow.

Codex remains an external reviewer whose GitHub review is consumed as an
artifact. The update does not enable automatic Claude-to-Codex or
Codex-to-Claude execution routing.

## Rationale

The installed subscription-authenticated Claude Code CLI accepted the full
official model ID in a bounded no-tools probe. Pinning the full ID makes the
model choice explicit and keeps the two project runners consistent.

AgDR-040 established that durable review artifacts are safer and more
verifiable than a session claiming it switched providers. That decision still
applies: `SB_REVIEW_BOT`, `review_response.bot_logins`, current-head review
checks, and bounded requeue markers remain the cross-model integration.

## Verification

- The installed Claude Code 2.1.270 probe returned `READY` with canonical
  model `claude-sonnet-5-5` and no error.
- The complete orchestrator suite passed before the edit: 1354 tests.
- A regression test covers both selectable shared templates and failed first
  while they still named the previous model.

## Consequences

- Existing subscription authentication is retained; no credentials or security
  settings change.
- The two WSL runners retain their existing per-project workflow and lock
  isolation; this is a model configuration update, not a new service topology.

## CIV-LIFE rollout gate

- Keep source issue #32 for PR #67 at status:human-review pending human-only AC11 and reproduction plus fixes for the save-write and carrier-ID validation findings.
- Do not treat that hold as a model-upgrade failure or alter #64, whose red migration blocker is intentional.
- These items are follow-up CIV-LIFE work after the Switchboard upgrade, not grounds to weaken review, requeue, or current-head guards.
