# AgDR-040 — Cross-model review by consuming an artifact, not by routing providers

**Status:** accepted
**Date:** 2026-08-15
**Supersedes the approach anticipated in:** `AgDR-039` (which recorded provider
routing as the intended mechanism)
**Builds on:** `AgDR-037` (review-response sub-poll)

## Decision

Cross-model review is achieved by **consuming an external reviewer's PR review**
— an artifact — rather than by routing Switchboard's own sessions across
providers.

Opt-in per project: `register-project.sh --review-bot <login>`, empty by
default. Setting it does three things:

1. Populates `review_response.bot_logins`, so the existing sub-poll
   re-dispatches the implementer while any of that bot's threads are unresolved
   (bounded at two rounds by the durable per-PR marker).
2. Names the bot in the QA prompt.
3. Makes the QA role **fail closed**: it must fetch that bot's review of the
   *current head sha*, confirm every thread is resolved, and cite it. Absent,
   stale, or pending → ESCALATE, never SHIP.

With no bot configured, QA runs same-model and says so on every verdict.

## Why not provider routing

Routing the `status:review` session to the opposite provider was the obvious
approach and is what `AgDR-039` anticipated. Three practical obstacles (the
strict `providers:` envelope rejecting the legacy top-level `claude:` block, a
host-side Codex login, the unfinished mixed-canary rollout review) were the
visible cost.

The decisive objection is different, and it generalises:

> **Provider routing would still leave the QA session self-reporting that a
> cross-check happened.** Consuming a review produces something checkable.

This project has already been bitten by exactly that. The prototype stance
shipped a QA prompt asserting it ran on a different model while no routing
existed, with a degradation disclosure gated on a condition that could never
fire — so every review would have claimed provenance it did not have, and
nothing could have detected it. A review that exists on a PR, at a sha, with
threads in a known state, cannot be claimed falsely without the claim being
falsifiable.

That is the principle worth carrying beyond this decision: **for a verification
rule, prefer the mechanism that leaves an artifact over the one that produces a
stronger guarantee on paper.** An attested claim you can check beats an
unattested claim you cannot, even when the unattested one is nominally stronger.

## Honest limitations

- **The ship DECISION is still same-model.** What became cross-model is
  finding *generation*, which is where the blind-spot value lives. A same-model
  session still decides whether to merge.
- **It depends on an external service.** If the bot is disabled, slow, or
  silent on a repo, QA escalates rather than merging. That is the correct
  failure direction, but it means a misconfigured project stalls at ESCALATE
  instead of failing loudly at load. Watch for it on first use.
- **"Every thread resolved" is the bar, not "no findings".** A bot that reviews
  and finds nothing is a pass; the loop cannot distinguish a considered pass
  from a shallow one.

## What would make this wrong

**If the external reviewer's findings are mostly noise.** The loop re-dispatches
the implementer on unresolved threads, so a low-signal reviewer converts
directly into wasted sessions — with the two-round cap as the only bound.

**If escalation-on-absence becomes routine.** If reviews are frequently late,
QA will escalate constantly and a human ends up in the loop for every ticket,
which is the autonomy this stance exists to provide, undone. The fix would be
waiting rather than escalating — deliberately not built, because a waiting QA
session burns budget doing nothing.

Early signal: the ratio of ESCALATE-for-missing-review to SHIP over the first
dozen tickets. If it is not close to zero, the timing assumption is wrong.
