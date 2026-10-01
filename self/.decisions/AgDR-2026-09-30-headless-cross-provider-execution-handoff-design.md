# AgDR-2026-09-30 - Headless Claude and Codex execution handoff design

**Status:** proposed - no production routing change
**Date:** 2026-09-30
**Scope:** a future bounded execution protocol for Claude and Codex workers. This record does not alter the current artifact-review integration.

## Decision

Do not enable general Claude to Codex or Codex to Claude execution switching in production until the protocol below is implemented and proven in a human-gated pilot.

The existing integration remains: an external Codex GitHub review is a durable artifact consumed by the Claude QA flow. It is not an execution handoff.

## Ownership model

The orchestrator owns one issue workspace, one issue branch, all status-label transitions, and terminal handoff. A worker owns only a leased turn and may write a proposed durable handoff artifact.

A provider change never transfers Git ownership directly. The next worker receives the same workspace only after the orchestrator has verified a clean boundary and has persisted an assignment record naming the next provider and expected head SHA.

## Durable handoff artifact

A provider-switch request must be an atomic workspace-local artifact containing:

- issue identifier and source provider;
- destination provider and a fresh monotonic handoff id;
- branch name and exact expected HEAD SHA;
- requested continuation purpose and bounded failure context;
- source session id, attempt count, and timestamp.

The orchestrator records acceptance in its durable assignment path before changing the issue claim. A missing, malformed, duplicate, or stale artifact is rejected with no provider switch.

## Same-head provenance

Before launching the destination provider, the orchestrator must verify that the workspace HEAD, open PR head, and artifact expected SHA agree. It must reject a dirty workspace, multiple open PRs, an unresolved handoff artifact, or any head change after the source turn.

The destination turn records the accepted handoff id and expected SHA in its result. A terminal handoff still requires the existing handoff-evidence contract, including issue, PR number, current branch head, and one open PR. This prevents a provider switch from laundering a stale or unrelated diff.

## Failure and recovery

No implicit fallback occurs. Provider failure, authentication loss, malformed JSONL, timeout, or resume failure leaves the issue in its current safe state with a classified diagnostic and the durable assignment intact or explicitly released.

Retry stays on the same provider and session where continuation is supported. A different-provider retry requires a new approved artifact and consumes its own bounded attempt. Ambiguous post-write state is reconciled by artifact id, branch head, and tracker read-back; it never guesses or starts a second worker.

## Enforceable merge safety

Codex currently has no verified PreToolUse-equivalent veto, so it cannot execute work for an agent-owned Gate C project. This refusal remains enforced in the selector.

The first execution pilot therefore remains human-gated and single-issue. Before any agent-owned Codex route, either the Codex CLI must expose and pass an independently tested repository-scoped merge veto, or the architecture must add an external enforcement layer that denies cross-repository merges and validates the command boundary. A prompt instruction is not sufficient.

## Pilot progression

1. Authenticate Codex interactively in WSL using subscription device login; do not copy tokens or use an API key.
2. Run the portable isolated pilot in dry-run mode, then its explicit Codex checkpoint with one docs-only issue and human Gate C.
3. Verify durable provider assignment, workspace cleanliness, same-head terminal evidence, one PR, and rollback to the unchanged Claude production binding.
4. Add a second provider only through an explicit handoff artifact fixture; keep all production weights and provider selection unchanged until that fixture passes.
5. Require review of the pilot evidence and this proposed record before any broader route.

## Queue follow-up kept out of this design

CIV-LIFE PR #67 remains at the human gate. Its queue follow-up must reproduce and fix two static findings before any release decision:

- world_save.gd opens the sole save path for write and does not check a failed write before reporting success;
- world_save.gd accepts carrier IDs without citizen, type, or route validation, allowing edited herd IDs to fail later in city generation.

These are gameplay/save correctness tasks, not execution-handoff work. Issue #64 remains intentionally red and is not altered by this work.

## Verification requirements

Acceptance requires deterministic fixture tests for every rejection path above, an authenticated but isolated human-gated pilot, and an adversarial review of command-boundary and merge denial behavior. Until then, the supported cross-model path remains review by GitHub artifact only.
