# AgDR-010: Worker turn budget 20→100; protocol read timeout 5s→30s

**Status:** accepted (2026-07-06)
**Surfaces:** `workflow/WORKFLOW.base.md` (claude block), composed `projects/switchboard-self/WORKFLOW.md`

## Context

Two independent session-killers surfaced while working #14 on 2026-07-06:

1. **`max_turns: 20` was structurally uncompletable for implementation-scale
   tickets.** Sessions burned 20 CLI-internal turns in ~7–9 minutes and exited
   `error_max_turns`. The failure path spawns a *fresh* session with no
   `--resume`, so every retry restarted from zero — #14 parked twice on this
   wall with no route to completion at any session count.
2. **`read_timeout_ms: 5000` killed real cold starts.** Five seconds to the
   first protocol line is under the real `claude` CLI's cold-start latency;
   two evidence-free instant failures at 19:17Z burned #14's session budget
   (2 of 3) in ~60 seconds without the agent ever running.

## Decision

Raise `claude.max_turns` to 100 and `claude.read_timeout_ms` to 30000 in the
base template and the composed switchboard-self WORKFLOW.md. Cost control
shifts from turn count to `max_budget_usd: 5` per invocation, which still
bounds every session; `turn_timeout_ms` and `stall_timeout_ms` are unchanged.

## Rejected options (steelmanned)

- **Keep 20 turns, add `--resume` on `error_max_turns`.** The correct
  structural fix — a resumed session loses nothing and the cap stays a
  diagnostic checkpoint. Rejected *as the immediate move* because it is
  scheduler code with failure-path semantics to design (what counts as
  progress, when to stop resuming); ticketed separately rather than rushed.
  The turn raise is a config-only unblock that stays safe under the budget cap.
- **Raise turns moderately (e.g. 40).** Avoids "100 is effectively unbounded"
  optics, but picks a new arbitrary wall with no evidence it clears real
  implementation sessions; budget already provides the real bound.
- **Keep 5s read timeout, retry instant failures for free.** Treats the
  symptom; a cold start would still need to win a 5s race, and "free" retries
  hide a misconfigured timeout instead of fixing it.

## Blast radius

Every future worker and triage session for every project composed from the
base. Worst case per session is unchanged in dollars ($5) but longer in
wall-clock before a doomed session fails. Parking (3 sessions/issue) still
bounds total spend per issue.

## Weakest point

`max_turns: 100` mostly delegates termination to the budget cap — a
low-token-burn loop (e.g. an agent retrying a denied command) now runs ~5×
longer before dying. If that pattern shows up in logs, the resume-on-cap
ticket becomes urgent rather than nice-to-have.
