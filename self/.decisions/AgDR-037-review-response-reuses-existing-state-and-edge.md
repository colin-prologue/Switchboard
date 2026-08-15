# AgDR-037 — Review-response reuses the existing gate state and re-entry edge

**Status:** proposed (ratify or overturn at the #43 merge gate)
**Issue:** #43 — Review-response worker: auto-triage and answer Codex comments
**Supersedes / amends:** nothing. Extends the AgDR-034 sub-poll shape.

## Context

Worker PRs accumulate bot review comments *after* the authoring session ends —
by the time Codex reviews, the issue is already `status:human-review`. Those
threads then wait for a manual pass (most recently the 2026-08-09 overnight
run: PR #132's six findings triaged, fixed and resolved by hand across three
codex passes). A PR reaching Colin at Gate C is therefore unreconciled with its
own bot review, and the reconciliation work is exactly the loop a session can
run.

The obvious shape — a new `status:review-response` state with a third session
role — is the expensive one. Every consumer in the round-2 verdict's eight-row
table would need a change, and the per-`(issue, role)` budget key would mint a
fresh budget that the round cap must then fight.

## Decision

**No new state. No new session role. No second scheduler.** The mechanism is two
existing things, reused:

1. **Trigger detection is an AgDR-034-shaped sub-poll** — bounded to
   `status:human-review` issues (skipping any also carrying `status:parked`),
   config-gated, per-issue reads. Unlike its prior art it is
   **write-bearing**: it writes the round marker, relabels, may post the cap
   comment, and resets the issue's session counters. That widening is granted
   here explicitly rather than inherited.

2. **Re-entry is the existing `human-review → todo` edge**, its actor widened
   from `human` to `[human, orchestrator]` with a `trigger: review-response`
   key. The re-dispatched session is an ordinary **implement-role** session on
   `todo`; all response behaviour lives in the prompt addendum. The third
   prompt branch, the third budget key, and the new-state consumer sweep all
   evaporate.

**ONE predicate drives both ends.** `needs_response(thread)` = the thread is
unresolved **and** its last external-bot comment postdates the last Switchboard
reply (or there is none). Trigger = any bound-PR thread satisfies it; DONE =
none does. Trigger and termination cannot diverge because they are the same
function. A human reply does *not* suppress the trigger — resolving the thread
does.

**Two identities, two sources.** Botness is `review_response.bot_logins`
(config, default `[]`). Switchboard's own identity is `$SB_APP_BOT_LOGIN`,
normalized (lower-cased, `[bot]` stripped — GraphQL returns the bare login for
a Bot author; verified live against PR #132). **Either one missing disables the
feature entirely, at zero API cost, with one log line.** Both gates run before
the issue fetch.

**The round cap is per-PR and durable**: a marker comment on the PR,
`<!-- switchboard:response-round n=N bots=... self=... -->`. Count = max `n`;
write = max + 1; cap = 2. Written *before* the relabel, so a crash between them
burns a round harmlessly rather than granting a free one. At the cap the
orchestrator **stops relabeling and comments** — it does not park.

**`self=` rides the marker** because the session cannot read the environment:
the worker allowlist has no `echo`, no `printenv`, no bare shell, and forbids
retrying denied variants. A denied read plus the unset⇒skip posture would be a
*silent disable*. Carrying the pre-normalized login on a marker the session
already parses is zero new capability.

## Rejected alternatives (steelmanned)

- **A new `status:review-response` state + a third role.** The honest modelling
  of a genuinely distinct activity, and it would make the board legible: an
  operator could see at a glance which PRs are mid-response. Rejected on blast
  radius — every dispatch/eligibility/role-pin/budget consumer changes, and the
  fresh per-role budget actively fights the round cap. The activity is
  distinct; the *machinery* it needs is not.
- **Prose `Closes #N` binding + PR-first scheduling.** Simpler to read, and it
  would catch PRs whose branch name drifted. Rejected: a structured binding
  already exists and is strictly stronger (`pullRequests(headRefName:)` +
  `closingIssuesReferences`, the same binding AgDR-028 handoff validation
  trusts), and PR-first scheduling is the second scheduler the ticket's
  non-goals forbid.
- **`.run/handoff-evidence.json` as the binding.** Rejected: workspace-local, so
  it does not survive workspace recreation.
- **Parking at the cap.** Rejected: no active `human-review → parked` edge
  exists, and `_park` clears only `IN_PROGRESS_LABEL`, which would strand a
  dual-label state. Stop-and-comment needs neither a new edge nor a `_park`
  generalization.

## Blast radius

- `workflow/transitions.yml` — the re-entry edge gains a second actor and a
  trigger key. `transitions.py` reads only `requires_marker`, so nothing
  executable changes; the table is documentation with a test.
- `WORKFLOW.base.md` + the composed project copies — **all implement sessions
  in all projects** read the new step 4. Its first line is a no-op guard keyed
  on the marker, so it costs 1–2 `gh` calls at turn 1 and then skips. Bounded
  and accepted.
- The relabeled `todo` keeps `gate:triage-passed`, so it is claimable without
  the sub-poll stamping anything.
- The trigger resets **every** role's session counter for the issue. Without it
  the multi-session PRs that attract the most findings arrive spent and park on
  the first response dispatch (#43 itself sat parked at 3/3). Bounded worst
  case: 2 × cap sessions.
- **Ships disabled.** Both carrying files land with `bot_logins: []`, so this is
  dead code at merge; going live is a deliberate config edit.

## Weakest point

**The termination guarantee is only as good as the session's discipline.** All
three triage branches must end with a post, because a thread left silent stays
owed forever — a correctly-*escalating* PR that forgot its in-thread pointer
would run straight to the cap. That rule lives in prose in the prompt addendum,
where no test can enforce it; the deterministic suite can only prove the
orchestrator's half. The round cap is what makes the failure bounded rather
than unbounded, and it is doing more load-bearing work than it looks like.

Second: a chatty bot that acknowledges every dismissal re-satisfies the
predicate, so under acknowledgment-heavy reviewers the cap comment becomes a
*normal* terminal rather than an anomaly signal. Accepted for now, bounded by
the cap, and worth revisiting if the signal degrades in practice.
