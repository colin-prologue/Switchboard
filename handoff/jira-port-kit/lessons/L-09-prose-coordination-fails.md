# L-09 — Prose-only coordination gets skipped; the incident that proved it

**Incident (2026-07-03).** Two PRs were in flight; the required merge
sequencing ("merge #23 before #24") was encoded only as prose in the review
thread. Both the agent and the human — each controlling part of the timing —
skipped it. Both PRs were green in isolation; merged out of order they
produced a semantically red main: tests asserting on attributes a parallel
PR had deleted. Classic deletion-racing-addition — an orphan-deletion at HEAD
cannot see concurrent consumers in parallel worktrees. A CI gate plus strict
branch protection then prevented the recurrence on the very next stale-merge
attempt.

**Lesson.** Prose binds only readers, and under timing pressure nobody is a
reader. Any constraint whose violation costs real recovery time must be
machine-checked: CI required checks, branch protection with up-to-date-branch
requirements, workflow-transition validators, dependency edges the scheduler
gates on. The proportionality threshold: the moment work touches overlapping
surfaces (two PRs near the same code, two daemons near the same ticket), the
enforcement is worth its friction.

**Why this is *the* multi-operator lesson.** Switchboard had one operator
providing accidental serialization, and still hit this. N engineers and N
daemons remove that accident entirely. Every "we'll just check the board
first" norm in your design is this incident waiting to happen — that's why
claiming is a verified CAS (I-13) and not a convention.

**Portable.** Entirely; it is constitution item I-1's origin story.
