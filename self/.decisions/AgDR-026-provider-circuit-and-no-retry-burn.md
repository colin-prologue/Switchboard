# AgDR-026: Provider circuits pause issue retry accounting

**Status:** proposed (2026-07-22)
**Surfaces:** scheduler provider health, issue retry/session accounting,
dispatch eligibility, lifecycle logs, shutdown/restart recovery, and Stage 7
rollout gates

## Context

Stage 7 Slice 1 established a conservative, closed `FailureClass` taxonomy and
provider-tagged lifecycle logs. It deliberately left behavior unchanged, so an
explicit provider authentication, plan, credit, rate-limit, or availability
failure still consumes an issue session and enters the issue retry/backoff path.

AgDR-016 rejects that behavior for subscription-backed Codex: provider
availability is an operator-level condition, not evidence that a ticket is hard
or broken. Repeating the same unavailable provider call can park otherwise
healthy issues and hide a provider-wide outage as several unrelated ticket
failures. AgDR-023 simultaneously forbids handing a dirty workspace to the
other provider.

Slice 2 needs a provider-scoped pause that is independently testable, leaves
healthy execution unchanged, and does not introduce a dashboard, daemon, or
tracker-based lock protocol.

## Decision

Add one process-local circuit state machine per configured provider. Only the
five explicit provider-availability classes accepted by AgDR-025 participate:

- `provider_authentication`
- `provider_plan_limit`
- `provider_credits_exhausted`
- `provider_rate_limit`
- `provider_unavailable`

`runner_startup`, `runner_timeout`, `runner_protocol`, `worker_failure`,
`provider_capacity`, and `assignment_refused` retain their current issue-level
behavior. The scheduler consumes the typed class only; it never reparses an
error string, transcript, or log message.

### Circuit states and transitions

Each provider starts `closed`.

- Authentication, plan-limit, and credit-exhaustion failures transition
  immediately to `open_latched`. No automatic probe is scheduled because these
  conditions require an operator action or an external allowance change.
- Rate-limit and generic availability failures transition to `open_cooldown`
  for a fixed five minutes. The duration is a code constant in the first slice,
  not workflow schema; tests may replace it with a short deterministic value.
- When the cooldown expires, the provider becomes `half_open`. Exactly one
  worker may enter as the probe; every other dispatch for that provider remains
  blocked.
- A successful probe closes the circuit. A probe that returns one of the five
  circuit-triggering classes reopens it using that class's latched or cooldown
  policy. A non-triggering outcome follows its ordinary issue behavior and
  closes the provider circuit because it supplies no continuing evidence of a
  provider-availability outage.
- A successful worker that was already running when another issue opened the
  circuit is also strong health evidence and closes the circuit. Opening a
  circuit never cancels another in-flight worker.

An operator resets an `open_latched` circuit by correcting the external
condition and restarting the process. A process restart starts circuits closed;
if the condition persists, the first typed provider failure reopens the circuit
without consuming that issue's retry or session allowance. This bounds restart
cost to one provider probe per process start without adding durable provider
health storage.

### No-retry-burn issue handling

When a running worker returns a circuit-triggering failure:

1. Keep its existing provider assignment and workspace.
2. Refund the session just reserved for that dispatch in
   `sessions_per_issue`; the value cannot fall below zero.
3. Do not increment the issue retry attempt, start an issue backoff timer, or
   evaluate the issue session-cap parking path.
4. Move the issue into a provider-wait collection carrying its existing issue,
   identifier, provider, and retry-attempt value. Keep the local claim while it
   waits, so the same process cannot double-dispatch its workspace.
5. Preserve `status:in-progress`: Switchboard still owns the claim and is
   explicitly waiting for its assigned provider. Do not add a tracker label or
   per-failure issue comment.

Provider waiters do not count against global, state, or provider running-worker
capacity. When the circuit closes, wake the scheduler and resume the oldest
waiters first, subject to the normal capacity and eligibility gates. A resumed
waiter uses the same retry-attempt value and provider; ordinary failures after
recovery resume the existing issue retry/backoff/session-cap behavior.

Normal tracker reconciliation also covers provider waiters. A waiter that
moves to a terminal state releases its claim and runs the existing terminal
workspace cleanup; a waiter that becomes non-active or loses a required label
releases its claim without dispatch. Recovery always re-fetches current issue
state before resuming, so stale queued data cannot resurrect human-completed or
gated work.

### Dispatch while open

Before any workspace, claim-status, session-counter, or durable assignment
write, dispatch asks whether the selected provider may start:

- An issue already carrying a durable `provider:*` assignment remains pinned
  and is refused while that provider is open.
- A new unassigned issue selected by operator label or weight remains unclaimed
  and unassigned while its selected provider is open. Because no workspace or
  assignment exists, a later reviewed routing configuration may still affect
  it under AgDR-023's existing unassigned-issue rule.
- Other providers continue dispatching normally. There is never automatic
  fallback, provider-label replacement, or cross-provider workspace handoff.

Repeated poll ticks may not emit repeated refusal noise for the same unchanged
circuit generation. The provider's open, half-open, and close transitions are
the primary operator signal.

### Stable circuit logs

Circuit transition lines add stable fields without exposing provider payloads:

- `provider_id`
- `circuit_state`: `open_latched`, `open_cooldown`, `half_open`, or `closed`
- `failure_class` on open/reopen transitions
- `cooldown_ms` on cooldown transitions
- `issue_id` and `issue_identifier` when a specific worker caused or probes a
  transition

A dispatch blocked by an open circuit uses `outcome=refused` and the circuit's
stored `failure_class`. Logs contain no prompt, model result, credential,
subscription balance, or raw provider diagnostic.

### Shutdown and restart

Circuit and provider-wait state are intentionally process-local. Shutdown does
not launch probes or convert provider waits into issue retries. On restart, the
existing startup claim sweep may restore a stranded `status:in-progress` issue
to `status:todo`; its durable `provider:*` assignment and preserved workspace
still force the same provider on redispatch.

No new tracker labels, files, external health service, or cross-process lock are
introduced. The existing single-runner-per-repository premise still applies.

## Independently testable delivery slices

1. **Pure circuit policy:** typed state/transitions, trigger allowlist,
   cooldown, latched reset, half-open single-probe gate, and sanitized logs.
2. **Scheduler no-retry-burn integration:** refund session accounting, preserve
   retry attempt and claim/provider ownership, queue waiters, and prove ordinary
   failures still retry and park exactly as before.
3. **Recovery and concurrency:** prove other providers continue, existing
   workers are not cancelled, only one half-open probe runs, waiters drain under
   capacity, and restart reopens a persistent outage without issue-cap burn.
4. **Isolated canary:** use only the synthetic mixed-canary repository with a
   deterministic fake provider failure. Prove circuit logs and recovery, then
   repeat the unchanged Claude-only rollback drill. Do not use an existing
   project or real subscription exhaustion as the first live test.

Every code slice requires focused tests plus the full orchestrator suite. Slice
4 additionally requires a named issue, retained logs, explicit stop condition,
and human confirmation before any low-percentage existing-project pilot.

## Verification gate

Implementation is acceptable only when tests prove:

1. Exactly the five provider-availability classes open circuits; near-match,
   unknown, runner, worker, capacity, and assignment outcomes do not.
2. A circuit-opening failure consumes neither issue retry attempt nor session
   cap and cannot park the issue.
3. Durable assignment and workspace ownership never change while waiting,
   probing, recovering, hot reloading, or restarting.
4. Terminal, gated, or required-label-removed waiters release correctly and
   cannot be resurrected when a circuit closes.
5. One provider's circuit does not block the other provider or consume its
   capacity.
6. Cooldown permits exactly one half-open probe and repeated failures cannot
   create a probe stampede.
7. Latched circuits require restart unless an already-running success supplies
   direct health evidence.
8. Healthy success, normal continuation, ordinary failure retry/backoff,
   session-cap parking, Claude-only launch, and immediate Claude-only rollback
   remain unchanged.
9. Circuit logs expose only stable fields and no credentials, prompts, model
   output, balances, or raw diagnostics.

## Rejected options

- **Fall back to the other provider.** A circuit is an availability boundary,
  not permission to transfer a potentially dirty workspace.
- **Park every affected issue.** Parking is an issue-level diagnostic checkpoint
  and requires human ticket action; a provider outage is shared infrastructure
  state and must not consume issue allowance.
- **Release and repeatedly redispatch the failed issue.** This recreates the
  retry storm under a different timer and makes `status:in-progress` dishonest
  between attempts.
- **Open on runner or unknown failures.** That expands blast radius beyond the
  conservative classifier contract and lets implementation defects pause a
  healthy provider.
- **Persist circuit state in GitHub labels or workspace files.** Provider health
  is process/operator state, not issue state; partial multi-issue writes create
  a harder recovery protocol than one bounded restart probe.
- **Add workflow-tunable circuit settings immediately.** Hot-reload semantics,
  invalid values, and per-project policy would enlarge the first behavior slice.
  A fixed initial cooldown is easier to test and revisit after canary evidence.
- **Cancel other running workers when one fails.** One issue's signal may be
  stale or localized; existing turns can provide the strongest recovery signal.

## Blast radius

Only typed provider-availability failure paths change. Healthy Claude-only,
Codex-only, and mixed workers execute through the same selection, workspace,
turn, continuation, capacity, and handoff paths. Project bindings and routing
weights do not change. Mixed execution against existing projects remains
prohibited until the isolated circuit canary and rollback drill pass.

## Weakest point

Circuit state is process-local, so restarting after an unresolved latched
failure launches one more provider probe. Persisting health would avoid that
call but would add stale-state and cross-process recovery problems. The initial
design accepts one bounded probe per restart because it refunds issue accounting,
preserves assignment, and immediately reopens on the same typed failure.
