# AgDR-2026-09-03 — Transient provider failures are bounded by attempts, not time

- **Status:** proposed (ratify at the adopting PR's merge gate)
- **Issue:** #195 (#194 is its closed parallel duplicate; the two load-bearing
  pieces of #194's design discussion are folded in below)
- **Date:** 2026-09-03
- **Supersedes / amends:** discharges the residual `AgDR-046` recorded against
  itself in its *Weakest point* section — "`max_sessions_per_issue` no longer
  bounds them, and nothing else does". Amends nothing in `AgDR-026`: the refund
  that record requires is untouched, and this ceiling is built so that it stays
  untouched.

## Context

`AgDR-026` says a provider outage is shared infrastructure state and must not
consume issue allowance. Before issue #166 it did: a transient provider failure
spent one of the issue's `max_sessions_per_issue` sessions, and civ-life #8 was
parked after five infrastructure failures having done no work and having been
charged for all of it. #166 fixed the accounting by refunding those sessions.

The fix was correct and it removed the only bound those retries had. The session
cap had been doing a job nobody assigned it. After #166 an issue whose dispatches
fail only on the provider is re-dispatched **forever**: the circuit-failure
branch refunds the session, does not increment `retry_attempt`, and moves the
issue to `provider_waiting`, from which `_resume_provider_waiters` re-dispatches
with the same attempt number. No attempt cap, no deadline, no elapsed-time
check. The only limits are per-invocation (`turn_timeout_ms`, `stall_timeout_ms`)
and neither sums across attempts.

This is not a hot loop — the circuit's 5-minute cooldown and single-probe
half-open recovery pace it to roughly twelve attempts an hour — which is why it
had not been noticed. Slow unbounded spend is still unbounded spend, and the
field evidence is not hypothetical: two verify sessions on civ-life #8 ran ~5
hours of wall-clock each and produced nothing. Under today's code both would
refund and retry.

The hard part is not adding a bound. It is that **"this provider keeps failing"
and "this work is legitimately long" are indistinguishable from the retry count
alone**, and a bound that confuses them re-creates #166's bug from the other
side.

## Decision

An owned extension `agent.max_transient_failures_per_issue` (default 6), keyed
by issue id alone, counting **consecutive transient provider-circuit failures**.
At the ceiling the issue parks through the existing `_park` path. Four
properties are the decision; the number is nearly incidental:

1. **It counts attempts, never elapsed time.** An attempt only happens when the
   circuit permits dispatch, so a bound made of attempts cannot accrue during an
   outage in which nothing is being attempted.

2. **Only the transient classes count** — `PROVIDER_RATE_LIMIT` and
   `PROVIDER_UNAVAILABLE`, the two that refund. A latched class (bad
   credentials, plan limit, exhausted credits) holds the circuit open until an
   operator fixes the provider; counting those would park every waiting issue
   for a condition the operator is already the fix for.

3. **Any *served* dispatch clears it.** Success clears it, and so does a
   non-circuit failure — because a non-circuit failure means the provider
   *answered* and the session reached the work. What is being bounded is a
   provider that will not serve this issue at all, so a ticket that flaps,
   recovers, works, and flaps again never accumulates toward a park.

4. **It parks; it does not route to the fail-review verifier.** The check sits
   *ahead* of the session cap in `_dispatch` precisely because an issue can be
   at both bounds at once, and the session cap's implement branch routes to a
   verifier session (issue #31). A verifier dispatched against a flapping
   provider strands exactly the way the sessions being counted here stranded.
   When the provider is what is failing, no agent is the answer.

**The default is deliberately looser than the bound #166 removed.** A transient
failure used to spend one of three sessions; a ceiling of six cannot re-park
anything that refund unparked. This ticket restores a bound, and is not
permitted to restore the behaviour.

**Restart behaviour, stated rather than implied:** the counter is process memory,
exactly like `sessions_per_issue`. A restart forgives an accumulating count; the
park it already produced is durable, because that lives in the `status:parked`
label. Durable session counts remain issue #15 and this deliberately does not
wait on them. Unpark clears the counter — not optional, because `_park`'s own
comment promises every counter resets on unpark, and a counter that survived
would make the documented remedy inert: the operator removes the label and the
issue re-parks on the very next dispatch.

**The park is reportless by construction, so the reason string is the whole
diagnosis.** Issue #16's cap-hit self-report fires inside `_worker`, keyed on
`BUDGET_CAP`/`TURNS_CAP`, while the session is live and before `after_run`
recycles the workspace. This ceiling trips at re-dispatch, when the session is
already dead, `after_run` has run, and the provider that would host a self-report
resume pass is the component that is failing. That is arguably correct — there is
no session to summarize and no provider to summarize with — but it means the park
comment must carry the distinction on its own, and it does: it names the count
and the ceiling, says this is a **provider outage and not an exhausted work
budget**, and states that none of those failures were charged to the issue's
allowance.

## Rejected options

**A wall-clock ceiling on the issue.** The obvious bound, and a concrete
disqualifier rather than a tradeoff. Steelmanned: it is the thing an operator
actually cares about (money and calendar time), it needs no taxonomy of failure
classes, and it bounds spend directly instead of by proxy. It fails on the
interaction with the circuit: during a multi-day latched outage every waiting
issue's clock ticks while **zero attempts occur**, and at the ceiling the
orchestrator parks every affected issue — the "park every affected issue" option
`AgDR-026` already rejected, wearing a timer. A variant that excludes
circuit-blocked time is defensible, but it is strictly more machinery
(instrumenting blocked intervals per issue) to reach the same place attempt
counting reaches for free, since attempts only happen when the circuit permits
them.

**A lifetime count of transient failures rather than a consecutive one.**
Simpler, and it bounds total spend rather than outage length, which is the thing
the consecutive form genuinely does not bound. Rejected because it parks
long-lived tickets for the provider's *history* rather than its current state: a
ticket that hit one rate limit in June and one in August would park in
September's outage with a lower remaining ceiling than a ticket filed yesterday,
for reasons having nothing to do with either the provider now or its own budget.
The residual is real and named below.

**Reverting #166's refund to get the bound back for free.** Explicitly a
non-goal, and worth stating because it is the cheapest possible fix. It is the
bug: it re-conflates provider health with issue allowance, which is the
conflation `AgDR-026` exists to forbid and the one that parked civ-life #8.

**Making the ceiling configurable off.** Rejected for the same reason the session
caps coerce: invalid or non-positive values coerce back to the default. A bound
that can be set to zero is the unbounded retry loop this field exists to close,
one config edit away.

## Blast radius

Scheduler dispatch path (one new check in `_dispatch`, ahead of the session cap),
the circuit-failure branch of `_handle_worker_done`, the unpark path, one new
`AgentConfig` field with the standard coercion, and the `_park` comment's counter
promise. Normative surfaces: `spec/SPEC.md` §4 gains the extension bullet,
`workflow/WORKFLOW.base.md` declares the field, `SETUP.md`'s restart paragraph
gains the counter.

Consumers of mutated state: the new counter is **in-process and read by the
scheduler alone** — no label, no marker, no board write. Its only durable output
is a `status:parked` label written through the existing `_park`, whose readers
are unchanged (dispatch eligibility and the board-state sanity check; the
Project-board sync is retired per `AgDR-048`, confirmed absent rather than
assumed). It is deliberately **not** reset by `_reset_issue_sessions`, so neither
the human-review re-entry grant (#178) nor the review-response sub-poll (#43)
touches it: reaching `status:human-review` requires a session that succeeded, and
success already clears the counter, so both would run against a provable zero.

Nothing about `_refund_issue_session` or the set of refunding classes changes.

## Weakest point

**A consecutive count says nothing about total spend, and that is the thing
somebody will eventually want bounded.** An issue that alternates
failure-failure-success indefinitely never reaches this ceiling and never gets
work done either, and the counter resets each time. The design accepts this
knowingly: the alternative that catches it (a lifetime count) parks healthy
long-lived tickets for ancient history, and the honest bound on total spend is a
cost ceiling, which is a different ticket and probably belongs with the Codex
per-run budget work rather than here. **What would make this wrong:** a ticket
that burns real money in an alternating pattern without ever tripping the
ceiling. If that shows up, the answer is a spend bound, not a bigger counter.

**Second, the default of 6 is a guess dressed as an argument.** The reasoning —
"looser than the three sessions #166 refunded, so it cannot re-park anything" —
establishes a floor, not a right answer. Six attempts at a five-minute cooldown
is roughly half an hour of provider failure before a park, which is short for a
genuine regional outage and long for a misconfigured project. It is configurable
precisely because this record cannot honestly claim to know. **What would make
this wrong:** a real outage that resolves in forty minutes and parks a board that
would have recovered on its own.

**Third, this bound is invisible until it fires.** There is no report, by
construction, and the counter is process memory, so an operator has no way to see
an issue at 5 of 6 — only the log line, and only if they are reading logs. A
restart erases the evidence entirely. The restart-loop case is the sharp edge: an
operator restarting *because* a provider is flapping is also, silently, resetting
the ceiling that would have told them how bad it was.
