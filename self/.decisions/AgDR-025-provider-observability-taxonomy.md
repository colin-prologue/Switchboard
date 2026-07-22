# AgDR-025: Provider observability taxonomy before circuit behavior

**Status:** proposed (2026-07-22)
**Surfaces:** provider adapters, neutral turn results, scheduler lifecycle logs,
failure fixtures, and Stage 7 rollout gates

## Context

Stage 6 proved that Claude and Codex can each complete explicit and automatic
assignments through one scheduler. The scheduler already includes `provider_id`
on dispatch and worker completion/failure logs. It does not, however, preserve
why a provider turn failed: Codex structured failures normalize to broad values
such as `codex_error` and `codex_turn_failed`, and the scheduler then treats the
result as an ordinary issue-level worker failure.

AgDR-016 requires subscription plan exhaustion, credit exhaustion, and
authentication expiry to be visible as distinct provider-availability
conditions. They must eventually stop consuming issue retries and must never
trigger cross-provider workspace handoff. Circuit behavior should not be built
on unreviewed string matching, so Stage 7 first needs a stable classification
and logging contract.

## Decision

Deliver Stage 7 in two independently reviewed slices. Slice 1 adds a neutral,
typed failure taxonomy and structured operator-visible logs while preserving
all current dispatch, retry, session-cap, parking, and fallback behavior. Slice
2 may use the accepted taxonomy for provider circuit state and no-retry-burn
handling. No existing project may use mixed mode between the slices.

### Stable lifecycle fields

Keep the current human-readable log messages and add stable fields to every
provider-scoped dispatch/refusal/terminal line:

- `provider_id`: `claude` or `codex` in real provider paths.
- `outcome`: one of `started`, `completed`, `failed`, `cancelled`, or `refused`.
- `failure_class`: present only for `failed` or `refused` outcomes.
- Existing issue, attempt, session, and error fields remain where available.

The initial `failure_class` vocabulary is closed:

- `provider_authentication`: persisted provider login is absent, invalid, or
  expired.
- `provider_plan_limit`: an explicit subscription/plan allowance signal.
- `provider_credits_exhausted`: an explicit purchased-credit exhaustion signal.
- `provider_rate_limit`: an explicit transient provider request-rate signal.
- `provider_unavailable`: an explicit provider availability signal that cannot
  be classified more narrowly.
- `provider_capacity`: the scheduler's configured per-provider slot refusal.
- `assignment_refused`: malformed or conflicting mixed assignment policy.
- `runner_startup`: executable missing or subprocess launch failure.
- `runner_timeout`: response, turn, or stall timeout.
- `runner_protocol`: malformed or incomplete terminal protocol.
- `worker_failure`: hook, tracker, implementation, or unknown turn failure that
  has no explicit provider-availability signal.

Use a typed enum or equivalent closed type in the neutral domain model. Keep
the adapter's existing normalized `error` value for detailed diagnosis; do not
make operators or scheduler policy parse that field after Slice 1.

### Classification ownership and false-positive posture

Provider adapters own provider-originated turn classification because they
understand their structured protocol. They classify the `provider_*` and
`runner_*` values produced while launching or running a turn. Prefer explicit
error codes, event types, or structured fields. A bounded diagnostic message or
stderr tail may be used only through a table-driven provider-specific
classifier with positive fixtures and near-miss/unknown fixtures.

The scheduler owns outcomes produced before or outside a provider turn. It
emits `provider_capacity` when a configured provider slot is unavailable,
`assignment_refused` when mixed assignment policy rejects an issue, and
`worker_failure` for scheduler-owned hook, tracker, or orchestration failures.
For a failed provider turn, it propagates the adapter's typed class without
re-parsing the adapter error or a human-readable log message.

Unknown or ambiguous failures classify as `worker_failure`. False provider
availability positives are more dangerous than false negatives because Slice 2
may open a provider circuit from this field. The scheduler must never infer a
class by matching human-readable log text.

Raw prompts, model output, credentials, and full provider payloads never enter
the lifecycle log. Existing bounded adapter diagnostics may remain in
workspace-local transcripts or the existing bounded `error` field. Logging
must retain the current 400-character sink cap and token-scrubbing invariants.

### Slice 1 behavior boundary

Slice 1 is observability only:

- no provider circuit or health state;
- no change to issue session accounting, retry delays, or parking;
- no automatic fallback or provider reassignment;
- no routing weight, workflow, project binding, or registration change;
- no new GitHub labels or per-failure comments;
- no HTTP server or metrics dependency.

The temporary consequence is explicit: a classified subscription failure still
uses the current retry/session-cap path until Slice 2. That is acceptable only
because mixed mode remains prohibited on existing projects. Slice 2 is required
before a low-percentage pilot and must satisfy AgDR-016's no-retry-burn rule.

## Verification gate

Slice 1 implementation requires:

1. Provider adapter fixtures for authentication, plan limit, credit exhaustion,
   rate limit, generic availability, startup, timeout, protocol, and unknown
   failures. Near-match text must remain `worker_failure`.
2. Shared contract tests proving failed turn results carry a valid closed
   `failure_class`, while successful results do not.
3. Scheduler tests proving provider-scoped started/completed/failed/cancelled
   and capacity-refused logs carry the stable fields.
4. Regression tests proving classification does not change retries, session
   caps, parking, assignment stickiness, or no-fallback behavior.
5. Sensitive-data tests proving provider diagnostics cannot expose injected
   GitHub credentials or subscription state in lifecycle logs.
6. The focused runner/scheduler/mixed suites and the full orchestrator suite.

## Rejected options

- **Add a dashboard or metrics service first.** A presentation layer cannot
  repair unstable or provider-specific outcome semantics.
- **Implement circuit breaking in the same slice.** This would make scheduler
  behavior depend on a new classifier before its false-positive boundary has
  independent review and regression evidence.
- **Use exception or log-message strings as policy.** Human-readable text is
  not a stable machine contract and may contain provider detail that should not
  be promoted into scheduler state.
- **Map every Codex error to provider unavailable.** Ticket, protocol, and
  provider-capacity failures require different operator actions.
- **Post issue comments for every provider failure.** Repeated retries would
  create tracker noise and expose infrastructure conditions on work items.

## Blast radius

Slice 1 changes only normalized in-process outcomes and structured stderr logs.
The same provider remains sticky, the same retries and session caps apply, and
all project launch modes remain unchanged. Production stays Claude-only.

## Weakest point

The exact structured signals emitted by subscription-authenticated Claude and
Codex may evolve, and the repository does not yet contain captured exhaustion
fixtures from both providers. Classifiers must therefore remain conservative;
unknown live signals stay `worker_failure` until a sanitized fixture and
reviewed mapping are added. This can delay circuit activation, but it cannot
incorrectly stop a healthy provider.
