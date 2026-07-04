# AgDR-008: Durable park via `status:parked` label

- **Status:** accepted (2026-07-04). Supersedes the in-memory-park weakness
  accepted in AgDR-002 (and its adversarial-audit addendum).
- **Context:** AgDR-002 parked issues in an in-memory set keyed on `updated_at`,
  with the per-issue session counter also in-memory. Its own weakest-point note
  called this out: a process restart forgets both, so a previously parked issue
  is re-granted the FULL cap (default 3) and re-dispatched — worst-case restart
  cost ≈ cap × per-session budget per parked issue. Live state on 2026-07-03:
  both dispatchable tickets (#10, #20) were already parked-at-cap, so restarting
  the pool would have silently re-dispatched exactly the issues Switchboard had
  decided need human attention.
- **Decision:** The park marker moves into the tracker as a durable
  `status:parked` label (provisioned in `register-project.sh`, written via a new
  `tracker.add_labels`). `_eligible` excludes any issue carrying the label, so
  the park decision is re-derived from the tracker on every poll and survives a
  restart for free. `active_states = [triage, todo, in progress]` already
  excludes `parked`, so the label also filters the issue out at
  `fetch_candidate_issues`; the explicit `_eligible` check is the robust,
  sort-order-independent gate. The in-memory `parked` set is demoted to a
  `set[str]` used only to reset the session counter on a within-run unpark.
- **Contract change (the contestable call):** the unpark *trigger* changes from
  "any `updated_at` bump (edit/comment/label)" to "a human removes the
  `status:parked` label." Stricter and deliberate — a stray comment no longer
  re-arms a capped agent — and it aligns with the board model (#22: drag the
  card off *Parked*). Confirmed with Colin before implementation.
- **Bonus — OBS-022 retired at the root:** the self-unpark loop (park comment
  bumps `updated_at` → next poll unparks → re-dispatch) is now *structurally
  impossible*: the park decision no longer reads `updated_at` at all. The
  `_park` claim-hold + post-comment re-fetch machinery that patched OBS-022 is
  removed. The FakeTracker regression guard is replaced by an `add_labels` fake
  that faithfully makes the label visible on subsequent fetches, plus a
  `test_parked_issue_not_redispatched_after_restart` that rebuilds the scheduler
  and asserts no re-dispatch.
- **Weakest point (accepted for this change):** the per-issue counter is still
  in-memory, so a restart *mid-issue but pre-park* (e.g. 2/3 sessions spent, not
  yet parked) re-grants a fresh cap. This is a strictly smaller residual than
  the one AgDR-002 accepted (it no longer applies to *parked* issues, only to
  in-flight ones), and was the explicit trade of the label approach over disk
  state persistence. `status:parked` is applied additively (the prior
  `status:todo` is not removed); single-status-column cleanup belongs to #22.
