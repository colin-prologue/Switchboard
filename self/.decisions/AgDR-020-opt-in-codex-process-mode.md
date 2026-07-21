# AgDR-020: Gate Codex dispatch behind an explicit process mode

**Status:** accepted (2026-07-13)
**Surfaces:** workflow provider parsing, CLI startup, runner selectors,
provider-neutral execution policy, running-entry reconciliation, and the Stage
5 canary boundary

## Context

Stage 4 proved that `CodexRunner` can execute and resume a subscription-backed
Codex CLI session, but deliberately left it unselectable. The scheduler still
read turn timeout, cumulative budget, and stall timeout from `cfg.claude()`, so
registering Codex would either validate the wrong workflow or apply Claude
policy to a Codex worker.

The first selectable Codex path must leave every existing invocation and
project binding Claude-only. It also must not expose the existing Switchboard
board to a second process: startup reconciliation is repository-wide, and the
current claim marker is visibility rather than a distributed lock. A local
probe showed that this host's `workspace-write` profile can create a git commit,
but official behavior permits `.git` protection in other environments.

## Decision

Add one explicit CLI switch, `--provider codex`, that installs
`CodexOnlyRunnerSelector`. The default remains `claude` and explicitly installs
`ClaudeOnlyRunnerSelector`, preserving all existing launch commands and project
bindings. A selector declares its process `provider_id`; startup and hot reload
validate the workflow for that provider before dispatch.

Codex mode accepts exactly one strict provider entry:
`providers.codex.kind: codex-cli`. Its optional fields are `command`,
`turn_timeout_ms`, `read_timeout_ms`, and `stall_timeout_ms`. Legacy Codex,
legacy Claude alongside Codex, mixed provider maps, unsupported kinds, unknown
fields, and empty commands fail closed. Claude mode retains the existing legacy
and dual-read behavior. One workflow cannot be switched between providers by
changing only the CLI flag.

Extend `AgentRunner` with execution policy values: turn timeout, stall timeout,
and optional cumulative dollar budget. Each adapter exposes those values from
its typed configuration; subscription-backed Codex has no dollar budget. The
scheduler requests a GitHub token whose TTL covers the selected runner's turn,
enforces the selected runner's cumulative budget, and copies the stall timeout
onto `RunningEntry` at dispatch. In-flight stall policy is therefore stable
across workflow reload, while later sessions use reloaded policy.

Stage 5A stops at fake-process integration and host-local git evidence. Stage
5B requires an operator-approved separate repository, confirmed GitHub App
installation access, and repeated real subscription-authenticated tickets.
Provider pooling remains Stage 6 work.

## Rejected options (steelmanned)

- **Infer Codex from `providers.codex`.** This removes a CLI flag, but makes a
  workflow edit sufficient to change execution identity and weakens rollback.
  An explicit process mode makes the operator's intent observable at launch.
- **Allow Claude and Codex entries in the canary workflow.** This would prepare
  the final schema sooner, but there is no routing or capacity policy yet. A
  mixed-looking file would promise behavior the scheduler cannot provide.
- **Add provider choice to each issue now.** Overrides are useful eventually,
  but sticky assignment across failure retries needs durable provider identity.
  A one-provider process preserves that invariant without inventing Stage 6.
- **Continue reading policy from workflow config in the scheduler.** A generic
  provider getter could work, but would duplicate selection and let policy drift
  from the runner instance actually dispatched. Runner-owned policy keeps the
  execution contract coherent.
- **Apply a reloaded stall timeout to running sessions.** This offers immediate
  operator control, but can retroactively kill or prolong work under a policy
  the session did not start with. Reloaded policy applies to future dispatches.
- **Use the existing repository for the live canary.** A required label can
  isolate dispatch but cannot isolate startup reconciliation. A separate
  repository is the smallest honest operational boundary.

## Blast radius

Existing CLI commands omit `--provider` and remain Claude-only. Existing legacy
and `providers.claude` workflows parse as before. No shipped workflow, project
binding, registration script, GitHub App installation, or production process
changes. Codex becomes reachable only when both the process flag and a strict
Codex-only workflow agree.

## Weakest point

Provider identity is process-sticky rather than durably claim-sticky. This is
safe while each process has exactly one accepted provider, but Stage 6 must
carry provider assignment across retries before mixed routing is enabled. The
local `.git` write probe is also non-portable; Stage 5B can fail even though the
adapter and scheduler tests pass, and must treat that as canary evidence rather
than weakening the sandbox.
