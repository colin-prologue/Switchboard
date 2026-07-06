# L-07 — Identity determines whether the merge gate is real

**Context.** Switchboard's solo operator initially ran agents on his own
token. That made the merge gate structurally weak: GitHub blocks approving
your own PRs, and agent PRs were "his" PRs. The fix was a GitHub App bot
identity — agent actions authored by the bot, so the human could formally
Approve. Also gained: clean provenance (bot vs. human actions) and separate
rate-limit budgets. The whole credential design followed: only long-lived
secret is a private key outside the repo; short-lived tokens minted per turn,
injected via env, never on disk, never logged; missing credentials fail
startup loudly rather than silently falling back.

**Lesson.** Decide identity *first*, because the merge gate's enforceability
falls out of it. The invariant is: approval must come from a human other than
the effective author, enforced by the forge (required approvals), not by
convention.

**L&W decision (already made — see TARGET-CONTEXT).** Operator identity:
agents act as the engineer who dispatched them. This is the *opposite* of
Switchboard's choice, and it works only because the team setting supplies
what the solo setting couldn't: a different engineer to approve. What you
must preserve deliberately since identity no longer does it for free:
- **Provenance:** agent work must be visibly distinguishable from the
  operator's hand-written work (branch namespace, PR template, commit
  trailer) — reviewers need to know what they're reviewing.
- **Enforcement:** required approvals must actually exclude the dispatching
  engineer (author-excluded approval counts).
- **Credential hygiene:** the per-turn-injection/never-on-disk discipline
  still applies to whatever tokens the daemon holds, even though they're the
  operator's own.
