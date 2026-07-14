# AgDR-018: Select one agent runner before dispatch claim

**Status:** accepted (2026-07-13)
**Surfaces:** `orchestrator/runner_selector.py`, scheduler dispatch, running-entry
observability, and scheduler integration tests

## Context

Stage 1 gave the scheduler a provider-neutral `AgentRunner` execution contract,
but the scheduler still imported and constructed `ClaudeRunner` directly in its
general component bundle. That made a second adapter require scheduler edits and
also caused runner instances to be built at call sites that only needed a
tracker or workspace manager.

Stage 3 needs an explicit provider-selection boundary without making Codex or a
mixed pool deployable. The boundary must carry enough issue context for later
deterministic routing, while preserving the current Claude-only behavior.

## Decision

Add an `AgentRunnerSelector` protocol with one operation:
`select(Config, Issue) -> AgentRunner`. `Orchestrator` accepts a selector through
constructor injection and defaults to `ClaudeOnlyRunnerSelector`, which always
returns `ClaudeRunner(cfg.claude())`. No other production selector is registered.

Select exactly once for each worker session, before the scheduler claims the
issue or writes `status:in-progress`, and pass that runner instance into the
worker's full turn loop. A selector failure therefore leaves tracker and claim
state untouched. Continuation turns in the same worker session use the same
runner instance.

Record the selected `provider_id` on `RunningEntry` and include it in dispatch,
session-start, completion, failure, cancellation, and stall logs. Tracker and
workspace construction remain separate from runner selection, and integration
tests inject fake runners through the selector rather than replacing the entire
component bundle.

Failure retries select again because provider assignment is not yet durable.
This is behaviorally safe while Claude is the only accepted provider. Sticky
provider assignment across retries is deferred to the mixed-pool stage, where
the retry record can carry the selected provider id deliberately.

## Rejected options (steelmanned)

- **Inject a `Callable[[Config], AgentRunner]`.** This is the smallest factory
  shape, but omits the issue context required for future explicit overrides and
  deterministic pool selection. Adding `Issue` now costs nothing and keeps the
  selection contract honest.
- **Build a provider registry now.** A registry would make Codex registration
  straightforward, but Stage 3 has only one valid provider and no pool policy.
  Shipping unused registry semantics would blur the safety gate and invite
  unsupported configuration.
- **Select after applying the claim label.** This keeps selection close to task
  creation, but a selector exception could strand an issue as claimed or
  `status:in-progress` without a worker. Selection is side-effect-free and must
  happen first.
- **Reuse one process-global runner instance.** This reduces construction, but
  future adapters may carry per-session state and hot reload must affect later
  sessions. One selection per worker session preserves those boundaries.

## Blast radius

The production result remains `ClaudeRunner(cfg.claude())` for both legacy and
provider-enveloped workflows. Claude command construction, credentials,
timeouts, budgets, prompts, retries, capacity, workspaces, and tracker behavior
are unchanged. The scheduler's internal component helper now returns only the
tracker and workspace manager.

## Weakest point

Runner construction is neutral, but several orchestration policies still read
`cfg.claude()` directly for token lifetime, budget, and stall limits. That is
acceptable while Claude is the only selectable runtime, but the standalone
Codex adapter and canary stages must introduce provider-neutral policy views or
runner metadata before Codex can be registered for dispatch.
