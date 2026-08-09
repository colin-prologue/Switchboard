# AgDR-033: fold signals bind by preceding-verdict rule; gate states get their own read path

**Status:** proposed (2026-08-08, issue #51 part a)
**Surfaces:** `workflow/WORKFLOW.base.md` worker branch (prompt template — the
AgDR trigger), `fold.py` binding/precedence rules, `tracker.py`
`fetch_open_issues_by_status` + `ISSUE_COMMENTS_QUERY`, `scheduler.py` `_tick`
sub-poll, `workflow.py` `fold.operator_logins` config surface

## Context

Every triage NEEDS-WORK / NEEDS-DECISION verdict is folded into the issue body
by hand (METHODOLOGY.md:38, "manual until #51"). This ticket is the DETECTION
half: notice an operator's approval on a `## Triage verdict` comment and surface
a validated signal. The apply step — CAS body edit, relabel, fold record — is
part (b), and part (a) performs **zero GitHub writes**.

Three ambiguities had to be closed before any code was useful, and none of them
were settled by the ticket:

1. **Binding.** GitHub issue comments have no reply threading — every comment is
   top-level. A `/fold` comment therefore does not intrinsically name what it
   approves. (A 👍 reaction does; it is the only intrinsically unambiguous
   channel.)
2. **Visibility.** Fold targets carry `status:drafting` / `status:decision`,
   which are **not active states**. `fetch_candidate_issues` filters to
   `cfg.active_states`, so it structurally never returns them, and
   `fetch_issues_by_states` is hard-wired to the CLOSED startup-cleanup query and
   returns `[]` for any open-state request. The poller's issue set was invisible
   to every existing read path.
3. **Dedupe.** Detection is a pure function of a comment thread, so a standing
   👍 re-resolves to the same signal on every poll, forever.

## Decision

**1. Binding rule.** A `/fold` // `/no-fold` comment approves the **most recent
`## Triage verdict` comment preceding it**. It MAY carry a `body-sha1:` line to
disambiguate; if that digest names no verdict on the issue, the signal is
**rejected and surfaced**, never silently re-bound to the latest verdict. A
reaction needs no rule — it is already comment-bound.

**2. Precedence, resolved per bound verdict** (not per issue): an explicit
comment beats any reaction on that verdict; among explicit comments the latest by
`created_at` wins; reactions decide only when no command is bound, where any
operator 👎 vetoes regardless of ordering.

**3. Visibility via a net-new read path.** `fetch_open_issues_by_status(names)`
reuses `CANDIDATE_ISSUES_QUERY` (already parameterized on `states`) against
`["OPEN"]` and applies the **same client-side `issue.state` filter**
`fetch_candidate_issues` applies, over the explicit list instead of the active
set. `active_states` is untouched, `fetch_issues_by_states` and its sole caller
are untouched, and reading a gate state has no dispatch side-effect.

**4. Dedupe is per PROCESS LIFETIME**, keyed on the deciding comment/reaction
node id. Restart re-emission is **accepted and documented**: part (b)'s durable
fold marker is the real dedupe, and a re-emission in part (a) costs one log line
because nothing is written.

**5. One canonical digest: `body-sha1`** (`git hash-object`) — the same value
Step 0 of the triage prompt computes and every post-#55 verdict carries. No
sha256 anywhere.

**6. Prompt-side prohibition.** The worker branch forbids agents from reacting
👍/👎 on or replying `/fold` to verdict comments. `operator_logins` already
filters by login; the prompt rule is the second layer, because an agent
approving the verdict on its own ticket defeats Gate A.

## Rejected options

- **Bind `/fold` to the latest verdict on the issue, unconditionally.**
  Steelman: simplest rule, no ordering logic, and in the overwhelmingly common
  single-verdict case it is indistinguishable from the chosen rule. Rejected
  because on a re-triaged issue it folds a verdict the operator never read: the
  operator scrolls to verdict N, types `/fold`, and a verdict N+1 posted in the
  interim silently steals the approval. The preceding-verdict rule makes the
  approval mean what the operator was looking at.
- **Silently re-bind an unknown `body-sha1:` to the latest verdict.** Steelman:
  more forgiving of a mistyped digest, and never strands an operator's intent.
  Rejected for the same reason, in its sharpest form — the operator explicitly
  named a target, so guessing a *different* one is worse than doing nothing. It
  is surfaced as a `RejectedSignal` so a typo is visible rather than swallowed.
- **Add `drafting`/`decision` to `active_states`.** Steelman: no new tracker
  method, and the tick already fetches candidates — the poller would get its
  issue set for free. Rejected outright: `active_states` is the **dispatch**
  set. Adding gate states would make the orchestrator dispatch sessions against
  issues parked at a human gate, which is the exact inversion of what Gate A
  means. A read concern must not be paid for with a dispatch change.
- **Widen `fetch_issues_by_states` to handle open states.** Steelman: one method
  instead of two, and its name already promises exactly this. Rejected because
  its sole caller is the startup terminal-cleanup sweep; changing its semantics
  puts a claim-reverting path in the blast radius of a read-only feature. The
  name is a misnomer worth fixing, but not in this PR.
- **`reactionGroups` instead of full reaction nodes.** Steelman: one field,
  smaller payload, and it answers "did anyone 👍 this?". Rejected because
  presence is not enough — the reacting login must be matched against the
  allowlist, and the per-reaction node id is the dedupe key. Pinned by a test.
- **Durable dedupe in part (a)** (a state file, or a marker comment). Steelman:
  no re-emission across restarts. Rejected as scope inversion: a marker comment
  is a **write**, which part (a) forbids, and a state file would be a second
  source of truth that part (b)'s marker immediately obsoletes.
- **Poll at the dispatch interval (30s).** Steelman: lower latency on approval.
  Rejected — an operator's 👍 is a human-latency event; polling it every tick
  spends one issue-comments query per gate issue per tick to shave minutes off a
  step that is currently measured in days. The sub-poll runs at 10 minutes, at
  the tail of a tick so it can never delay dispatch.

## Blast radius

Additive. No existing method's behavior changes: `active_states`,
`fetch_candidate_issues`, `fetch_issues_by_states`, dispatch eligibility, and the
verdict format (#55 owns `body-sha1:`) are all untouched. With the shipped
default (`fold.operator_logins: []`) the feature is **entirely inert** — the
sub-poll returns before its first API call, so a project that never configures
`fold:` pays nothing and cannot be affected. When enabled, the only observable
effects are GraphQL **reads** and `fold signal detected` log lines. Malformed
`fold:` config fails loudly at startup via `validate_dispatch`, because a
silently-disabled approval channel is indistinguishable from "the operator hasn't
reacted yet".

## Weakest point

**The preceding-verdict binding rule is a guess about operator intent, and the
`created_at` ordering it rests on is not the ordering the operator saw.** The
rule assumes the operator's `/fold` was typed while looking at the newest verdict
above it — but comments are ordered by server timestamp, and an operator who
opens the issue, reads verdict N, gets interrupted, and posts twenty minutes
later binds to verdict N+1 if one landed meanwhile. The `body-sha1:`
disambiguator is the escape hatch, but it is **optional**, so the common path is
the guessing path. Two things bound the damage rather than eliminate it: the
reaction channel (unambiguous by construction) is the one operators are most
likely to use, and part (a) writes nothing, so a mis-bound signal in this PR is a
wrong log line, not a wrong body edit. That protection **expires when part (b)
lands** — before the apply step consumes these signals, either the digest should
become mandatory on the comment channel, or the apply step must re-verify the
binding against the body it is about to overwrite (the CAS read makes this
cheap). Recording that here so part (b) inherits the obligation rather than the
assumption.
