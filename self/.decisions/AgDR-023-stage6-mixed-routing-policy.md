# AgDR-023: Stage 6 mixed-provider routing policy

**Status:** proposed (2026-07-17)
**Surfaces:** mixed-process CLI mode, workflow schema, selector, scheduler,
provider labels, capacity accounting, and Stage 6 canary

## Context

Stages 1 through 5B established a neutral runner contract, dual-read Claude
configuration, a standalone Codex adapter, and isolated native-terminal Codex
evidence. The existing runtime is intentionally still process-wide:
`--provider claude` selects `ClaudeOnlyRunnerSelector`, `--provider codex`
selects `CodexOnlyRunnerSelector`, and the workflow parser rejects a map
containing both providers. That keeps the production Claude process safe, but
does not yet provide a policy for assigning one issue to one provider.

Stage 6 must make that assignment deterministic and durable. A retry, restart,
or workflow reload must not hand a partially modified workspace from one model
to another. The default Claude-only command must remain an immediate rollback.

## Decision

Introduce an explicit opt-in `--provider mixed` process mode. The existing
default stays `--provider claude`; `--provider codex` remains the one-provider
canary mode. Mixed mode starts only when a workflow supplies complete,
validated `providers.claude` and `providers.codex` blocks plus a valid `routing`
block.

### Assignment order

1. A durable `provider:claude` or `provider:codex` label wins. It is the
   system-owned record of an assignment already made for this issue.
2. For an unassigned issue, one explicit operator label, `agent:claude` or
   `agent:codex`, wins.
3. Otherwise, select from `routing.weights` by applying a stable SHA-256 hash
   of the immutable issue node ID to the cumulative positive weight range.
   This is deterministic across process restarts and does not use random state.

Before claiming an unassigned issue, write the selected `provider:*` label.
Only after that write succeeds may the scheduler apply `status:in-progress` and
start a worker. The assignment label remains after handoff or closure as audit
history; an operator may remove it while the issue is inactive to deliberately
route a fresh engagement. A late `agent:*` label never changes an existing
`provider:*` assignment.

Conflicting `agent:*` or `provider:*` labels, an explicit request for a
provider absent from the validated workflow, or a malformed routing policy are
pre-claim refusals: leave the issue unclaimed, write at most one diagnostic
comment, and never silently fall back to the other provider.

### Configuration and capacity

Mixed workflows use this new, opt-in envelope:

```yaml
routing:
  weights:
    claude: 100
    codex: 0

agent:
  max_concurrent_agents: 4
  max_concurrent_agents_by_provider:
    claude: 4
    codex: 1

providers:
  claude:
    kind: claude-cli
  codex:
    kind: codex-cli
```

Weights are non-negative integers with at least one positive value and may name
only configured providers. `max_concurrent_agents_by_provider` is optional; a
missing provider cap means the global `max_concurrent_agents` is the cap. A
provider cap can reduce, never raise, the global limit. The initial mixed
configuration must use `claude: 100, codex: 0`; moving Codex above zero is a
separate reviewed rollout change.

The scheduler counts running entries by their durable provider assignment in
addition to its existing global and state caps. Continuations, failure retries,
stall retries, and restart recovery resolve the stored `provider:*` label and
therefore retain the original runner. A valid hot reload affects only issues
that have not yet received a provider assignment; an invalid reload retains the
last known good policy under the existing reload contract.

### Failure and rollback posture

There is no automatic provider fallback. A pre-claim unavailable-provider
condition produces one diagnostic and no workspace. A worker failure after
assignment follows the existing retry/backoff/session-cap parking behavior with
the same provider. This prevents a second model from inheriting uncommitted
work from the first.

Rollback is operationally simple: stop the mixed process and launch the
unchanged Claude-only command. `provider:*` labels preserve the assignments for
diagnosis; they do not make the Claude-only process select Codex.

## Independently testable delivery slices

1. **Schema and CLI boundary:** add `mixed` as an explicit CLI mode; parse and
   validate both provider blocks, routing weights, and provider caps while
   proving existing Claude and Codex one-provider workflows are byte-for-byte
   behaviorally unchanged.
2. **Deterministic selector:** implement the assignment precedence, hash bucket
   selection, conflict refusal, and durable `provider:*` write before claim.
   Test identical selections across fresh selector instances and restart-shaped
   fixtures.
3. **Capacity and stickiness:** enforce per-provider caps alongside global and
   state caps. Test continuations, failure/stall retries, unpark, hot reload,
   and a process restart preserve the original provider without cross-provider
   fallback.
4. **Isolated mixed canary:** create a separate synthetic mixed-provider
   repository and binding. First prove explicit Claude and Codex assignments
   independently, then a `claude: 100, codex: 0` no-op rollout, and only then a
   reviewed nonzero Codex weight. Keep production Claude-only processes off the
   mixed workflow throughout.

Every slice requires focused tests plus the full suite. The final slice also
requires native-terminal evidence for Codex, a named operator stop condition,
and a rollback drill to the unchanged Claude-only command.

## Rejected options

- **Select randomly at dispatch.** Randomness makes assignment hard to replay,
  audit, or preserve after a process restart.
- **Keep assignment only in scheduler memory.** A restart could re-route a
  dirty workspace to the other provider.
- **Use `agent:*` as both override and system assignment.** An operator cannot
  distinguish a deliberate override from an automatic choice, and changing the
  label during an active engagement risks a silent handoff.
- **Fallback from Claude to Codex or vice versa after a worker error.** This
  crosses the workspace ownership boundary exactly when diagnostic evidence is
  most important.
- **Enable a nonzero Codex weight with the schema change.** Schema acceptance
  must not itself alter production dispatch behavior.

## Blast radius

No current production process changes: its omitted `--provider` flag still
selects Claude, legacy `claude:` workflows remain valid, and the Codex canary
continues to use its Codex-only binding. Mixed mode requires an explicit CLI
flag, both provider configurations, provisioned `provider:*` labels, and a new
isolated canary binding.

## Weakest point

The durable assignment-label write occurs before the status claim, so a tracker
outage can leave an inactive issue assigned but unstarted. That is intentional:
it is observable, safe to retry, and never grants a later provider a different
workspace owner. The implementation must make this state visible and document
the operator action for deliberately clearing an inactive assignment.
