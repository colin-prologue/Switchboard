# AgDR-046: CLI-authored typed codes classify the failure; a latched circuit tells the operator

- **Status:** proposed (2026-08-23). Issue #165. **Amends AgDR-032**, which
  rejected reading the assistant record's typed code; that rejection is answered
  by a discriminator it did not consider, not waived.
- **Surfaces:** `runner.py` (stream harvest + both terminal branches),
  `failure_classification._CLAUDE_CODES` / `_TEXT_PATTERNS`,
  `scheduler._on_worker_done`, `tracker.find_or_create_ops_issue`,
  `tests/fixtures/README.md`

## Context

Five consecutive verify sessions on civ-life #8 (2026-08-16 → 08-19) failed on
infrastructure — two `API Error: Connection closed mid-response`, three
`Failed to authenticate: OAuth session expired and could not be refreshed` —
and Switchboard charged **all five** to the ticket's verify budget, then parked
it as though the review had failed. PR #16 carries zero review comments: not one
of those sessions reviewed anything.

Every mechanism needed to prevent that already existed and was correct.
AgDR-025/026 built the taxonomy, the latch/cooldown split, and
`_refund_issue_session` ("give back the session a provider-circuit failure
burned"). `_circuit_refusals` even dedupes the refusal notice per circuit
generation. None of it ran, because `classify_claude_failure` returned
`WORKER_FAILURE` for both strings.

Two gaps behind that:

1. **The typed code was excluded by decision.** AgDR-032 saw
   `"error": "authentication_failed"` on the assistant record and rejected using
   it: *"it makes model-adjacent stream content a circuit input: any assistant
   message could then latch the provider."* It classified on the terminal
   record's `result` prose instead, which worked for the one captured condition
   because that prose says "Not logged in". OAuth expiry says something else.
2. **`_CLAUDE_CODES` has never matched real output.** The only captured
   `terminal_reason` is `api_error`, absent from the map — so every real Claude
   classification to date ran on `_TEXT_PATTERNS` alone. The map carried the
   same "forward-compatible only" property documented for `_CODEX_CODES`,
   undocumented here.

Separately: the latch is the one condition that stops all work and cannot clear
itself, and its only output was a log line. Switchboard is operated in **waves**
— queue work, walk away for a week — so a terminal is not a channel.

## Decision

**1. `message.model == "<synthetic>"` is the trust boundary, and it is what
makes the typed code readable.** AgDR-032's objection is about *authorship*, not
about the field. `model` is CLI-assigned; only the CLI's own synthetic error
records carry `"<synthetic>"`, and the logged-out fixture shows the marker and
the typed code on the same record. A real model turn keeps its real model id, so
model-authored content still cannot reach the circuit. `_synthetic_error_code`
returns "" for everything else, and
`test_a_non_synthetic_record_claiming_a_provider_code_is_ignored` pins a record
carrying `error: "authentication_failed"` under a real model id as a success.

**2. A standing synthetic error is cleared by any subsequent real model turn.**
The code is held, not applied on sight. If the CLI retried and got through, work
landed and the turn is a success — failing it would be a new bug of the same
family as the one being fixed.

**3. Both terminal shapes are handled, because only one is captured.** On a
gated record the typed code takes precedence over `terminal_reason`. On an
`is_error`-absent record a standing code that classifies into
`CIRCUIT_FAILURE_CLASSES` fails the turn on its own. The second branch exists
because the transcripts do not contain a `result` record and neither condition
is reproducible on demand — the shape is genuinely unknown, and guessing it the
way #116's single capture suggests is what produced this ticket. Ordered AFTER
the `error_max_turns` branch, for AgDR-027's reason.

**4. `server_error` maps to `PROVIDER_UNAVAILABLE`, not an auth-shaped latch.**
AgDR-032 rejected a blanket `is_error ⇒ PROVIDER_AUTHENTICATION` rule precisely
because latching a 5xx is a worse outage than the bug it fixed. That reasoning
is adopted, not overturned: transports drop, and the cooldown heals them.

**5. A latched circuit posts one comment to a per-project ops issue.** Latched
classes only — auth, plan limit, credits. Edge-triggered on
`(provider, generation)`, mirroring `_circuit_refusals`, so a standing latch
never becomes a heartbeat. Transient failures are deliberately silent: a channel
that also carries recoverable noise is one the operator learns to ignore, and
that would cost more than it buys in a wave-operated system.

The ops issue is found-or-created by **title**, carries no `status:*` label
(`normalize_status_state` → `"none"`, in no stance's active set, so it is never
dispatched to an agent), and needs no per-project setup. A notice that silently
goes nowhere because a project skipped its configuration is the failure mode
this whole change removes. A failed post un-marks the notice so a later latch
retries rather than inheriting the silence.

## Rejected options

- **Add the two prose strings to `_TEXT_PATTERNS` and stop.** Steelman: one
  line, no trust-boundary argument, ships in ten minutes. Rejected as the
  primary mechanism because it is the same bet #116 made — that the next wording
  will resemble the captured one. The patterns are still added, as the fallback
  for a CLI that drops the typed code; they are just not what the fix rests on.
- **Presume any zero-artifact session is an infrastructure failure and refund
  it.** Steelman: covers wordings and codes nobody has seen, which is the real
  shape of this problem. Rejected *for now*: with no attempt cap and no
  wall-clock bound anywhere, a genuinely stuck agent would retry forever. It
  becomes viable once a session timeout exists, and is the honest long-term
  answer to the residual risk below.
- **Notify on the transient classes too.** Rejected: they self-heal, so the
  notice would arrive after the problem is gone and train the operator to ignore
  the channel that carries the latch.
- **Configure the ops issue per project (`SB_OPS_ISSUE`).** Steelman: no new
  tracker write surface, no `createIssue` mutation. Rejected: it fails silent
  when unset, on exactly the projects whose operator is not watching.
- **Post the notice on the refused issue.** Steelman: zero new concepts, the
  path `add_issue_comment` already serves. Rejected on the operator's side of
  the trade: notices scatter across whichever tickets happened to be dispatched,
  land on tickets that are not at fault, and give no single place to check after
  a wave.

## Revisions from review (codex, PR #166)

Three findings, all adopted; two changed the decision rather than the code.

- **The standing code now reaches the general failure branch.** The first pass
  wired it into the `is_error` gate and the success branch only, so a synthetic
  provider error followed by a terminal record with a *failure* subtype still
  classified on the subtype alone. That is the same defect this record exists to
  fix, on the branch it had not reached — an instance of the bet decision 3
  explicitly refuses to make. Safe against `error_max_turns_no_session`, because
  reaching a turn cap requires real model turns and any one of them clears the
  standing code.
- **The notice retries for real.** The first pass un-marked the key on failure
  and called that a retry. It is not: `record_failure` returns `None` once the
  circuit is latched, and a latched circuit refuses every dispatch, so nothing
  re-enters `_start_latch_notice` — the notice was lost for the life of the
  process. Retry is now bounded (3 attempts, 30s apart) inside the notice task,
  where the only remaining trigger is. The key is still released afterwards so a
  later generation is not silenced by this one's bookkeeping. The test that
  claimed to cover this passed vacuously and leaked a pending task; it now waits
  on the observable rather than on a task set that is empty both before the task
  is created and after it ends.
- **The recovery hint is derived from the failure class**, not hardcoded. A
  credits or plan-limit latch, and any auth latch on a non-Claude provider, was
  being told the Claude CLI needed a fresh login — an operational instruction
  pointing at the wrong credential while every project stayed stopped.

## Blast radius

Claude runner only; codex untouched. `_CLAUDE_CODES` gains two entries that
previously matched nothing, so no existing classification changes. Turns that
previously returned `succeeded` while a standing synthetic provider error was
outstanding now fail — which is the point: pre-#165 they spent a session and
called `record_success()`, resetting the circuit on a turn that did no work.
One net-new tracker write surface (`createIssue`), a sanctioned §11.5 exception
on the same grounds as `add_issue_comment`.

## Weakest point

The code values are **transcript-derived, not probe-captured** — the honest gap
is recorded in `fixtures/README.md` rather than papered over with a
hand-authored fixture, per #109's rule. Neither condition is reproducible on
demand, so the `is_error`-absent branch is written against a shape nobody has
observed end to end. It is defensive, and defensive code that never fires is
code that rots.

The larger residual is unbounded retry. Refunding transient failures means
`max_sessions_per_issue` no longer bounds them, and nothing else does: there is
no attempt cap and no wall-clock ceiling in the codebase. Two of civ-life #8's
sessions hung ~5 hours each. The cooldown paces retries to roughly one per five
minutes, so this degrades into slow spend rather than a hot loop — but it is
genuinely unbounded, and it is the reason the zero-artifact rule above is
deferred rather than adopted. A session timeout should land before this system
is left running unattended for a long wave.
