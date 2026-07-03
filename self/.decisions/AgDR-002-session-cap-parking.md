# AgDR-002: Session cap + parking (one tracker-write exception)

- **Status:** accepted (autonomous run, 2026-07-01) — **most contestable call
  of the run; review this one first.**
- **Context:** Core Symphony re-dispatches an active issue indefinitely
  (normal exit → 1s continuation retry → new session). The Codex original
  bills differently; with `claude -p` each session is real money. An agent
  that never moves the status label = unbounded spend. Separately, the plan's
  guardrail said "orchestrator never writes the tracker (core §11.5)", but the
  approved plan's cap test expects a parking comment on the issue.
- **Decision:** Owned extension `agent.max_sessions_per_issue` (default 3).
  On exhaustion the orchestrator *parks* the issue: claim released, workspace
  + logs preserved (caps are diagnostic checkpoints, not kill switches —
  HDR-lineage from Switchboard v2), posts ONE notification comment, and skips
  the issue until its `updated_at` changes (human touched it → unpark + reset
  counter). The comment is the single sanctioned exception to the §11.5
  no-tracker-writes boundary, documented in spec/SPEC.md §4.
- **Why the exception:** at parking time nothing else is alive to tell the
  human; a silent park in a log nobody watches defeats the checkpoint purpose.
- **Weakest point:** parked-state is in-memory; a process restart forgets it
  and will burn one more session before re-parking. Accepted for v1
  (restart recovery is tracker-driven by design, core §14.3).

## Addendum (2026-07-03, adversarial audit)

- **Blast-radius correction:** the original weakest-point claim ("one more
  session") was 3× optimistic. `sessions_per_issue` is in-memory alongside
  `parked`, so a restart re-grants the FULL cap (default 3) to a previously
  parked issue before it re-parks. The risk stays accepted for v1, but at its
  actual size: worst-case restart cost per parked issue ≈ cap × per-session
  budget, not one session.
- **Cap is always-on:** `max_sessions_per_issue <= 0` (or non-int) coerces to
  the default rather than disabling parking — an unbounded-spend
  configuration cannot be expressed. (Previously the scheduler carried a
  dead "cap disabled" branch that config coercion made unreachable.)
- **Park race closed:** the orchestrator now holds the issue's claim while
  the parking comment posts and the post-comment `updated_at` marker is
  re-fetched; a poll tick landing in that window could previously unpark and
  burn a bonus session past the cap.
- **Wording fix:** parking preserves the workspace and the `after_run` run
  log beside it; there are no per-issue orchestrator logs (the earlier
  "workspace + logs" phrasing overpromised — the log sink is stderr).
