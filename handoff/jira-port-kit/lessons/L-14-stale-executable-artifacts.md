# L-14 — Stale executable artifacts fire faithfully at invalidated targets

**Incident (2026-07-05, sibling project).** A pre-staged implementation
goal-prompt still described the *old* architecture after a design pivot. The
staleness warning existed — but in a branch-local resume doc the executor
never read. The prompt would have executed faithfully against the dead
design; it was caught only because a human asked "is this still right?" The
warning was then moved to the launch ticket — the executor's reading
altitude — and the principle recorded.

**Lesson.** The property that makes artifacts good for delegation — faithful
execution without re-deriving context — is exactly what makes stale ones
dangerous. Two rules:
1. **A pivot invalidates downstream artifacts, actively.** When a spec,
   model, or contract changes, re-point or explicitly invalidate every
   kickoff ticket, goal-prompt, and runbook that depends on it. The trigger
   is a pivot, not routine edits — auditing everything on every edit is
   ceremony.
2. **Warnings live at the executor's altitude.** A staleness note the
   executor won't read (branch doc, wiki page) has near-zero execution
   probability. Put it in the ticket the daemon dispatches from.

**Where it bites the Jira port.** Your tickets ARE pre-staged executable
artifacts, consumed by daemons possibly days after writing, after other
tickets have changed the ground truth. The dispatch-time reconciliation
(I-14) covers *board* staleness; this lesson covers *content* staleness —
consider a cheap mechanized freshness check (e.g., ticket references specs
by version, triage re-runs after long parks) rather than trusting prose
reminders.

**Portable.** Entirely. Note this kit is itself such an artifact —
TARGET-CONTEXT.md is where its own staleness gets managed.
