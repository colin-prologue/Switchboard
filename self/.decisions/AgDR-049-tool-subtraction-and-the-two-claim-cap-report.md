# AgDR-049 — Tool-lessness is a subtraction on a different flag, and the cap-hit report carries two claims it never reconciles

**Status:** proposed (ratify or overturn at the #16 merge gate)
**Issue:** #16 — Cap-hit post-mortem: bounded summary pass before a session gives up
**Supersedes / amends:** amends nothing. Consumes #169's taxonomy contract
(`failure_taxonomy`) and feeds #31's fail-review verifier (AgDR-047), whose
evidence tier 4 was empty by construction until now.

## Context

The fail-review verifier reads evidence in four tiers and its fourth —
"self-reports LAST — any summary the failed session wrote about its own
failure" (`workflow/WORKFLOW.base.md:424`) — had no producer. The prompt calls
the disagreement between a self-report and the mechanical signals "often the
most useful line in the verdict" (`:426-427`), and that line could never be
written, because there was no self-report to disagree with.

#16 makes the capped session write one: at the turn-loop break, while
`session_id` is still a live local, one bounded tool-less resume, posted as its
own `## Cap-hit report` comment.

Three questions had no precedent and had to be decided here. The first was
named in the ticket as a pre-dispatch blocker; the other two are the shape of
the artifact.

## Decision

### 1. "No tools" is `--tools ""`, a subtraction on a different axis — not a narrowed `--allowedTools`

`runner._build_command` composes the provider command by **appending** to the
operator-configured `command` string, which already carries `--allowedTools`.
"No tools" is a subtraction, and a builder that can only append cannot express
one: appending a narrower `--allowedTools` does not revoke the grant already
on the line.

`--tools` is a different axis. It selects the *available* set from the built-in
tools, and the installed CLI documents `""` as "disable all tools" — verified
against `claude --help` at implementation time and pinned by
`test_runner.py::test_the_summary_pass_flag_means_disable_all_tools_on_the_installed_cli`,
which asserts the CLI's own help text rather than our belief about it. An empty
available set cannot be repopulated by an `--allowedTools` grant, because a
grant is permission to use a tool that *exists*.

`--max-turns 1` rides alongside it. That is a cap on the CLI's internal loop:
with no tools there is nothing to iterate on, so the cap is the belt to that
braces.

Two runner tests pin the scope: the summary pass carries `--tools` with `""`
surviving as **its own argv entry** (a value joined away would read as
`--tools --max-turns`, silently making the flag a no-op and the "tool-less"
pass fully tooled), and an ordinary turn carries no `--tools` at all.

### 2. Summary capability is a separate Protocol the scheduler asks about, not a defaulted method

`SummaryCapableRunner` is its own `@runtime_checkable` Protocol beside
`AgentRunner`. The scheduler branches on `isinstance`, never on a provider-id
string.

A defaulted `run_summary_turn` on `AgentRunner` would answer "yes, I can do
this" for every adapter that never implemented it, and the scheduler's whole
need here is to *ask*. Codex has no budget ceiling to report on at all
(`codex_runner.py` hard-wires `max_budget_usd = None`; issue #181), so it
deliberately does not implement the method — and a provider that cannot
self-report still gets a report, built from the mechanical fallback, with
`has no summary pass` as the stated reason.

### 3. The report carries `self_reported` AND `mechanical`, and where they differ the disagreement is rendered, not resolved

The mechanical class is a **coarse prior, not a verdict**: it knows exactly one
thing, which ceiling fired. Budget exhaustion renders `quota`; turn exhaustion
renders `iteration`. Either can be wrong — a session that spent every turn
going in circles may well have burned its budget doing it.

So the YAML block carries both fields plus an explicit `agreement:`
(`agree` | `disagree` | `unavailable`), and a disagreement is stated in prose a
human reads as well as a field a parser reads. Nothing in this module picks a
winner. Picking one would delete exactly the signal the verifier exists to
weigh — and would do it with strictly *less* evidence than the verifier has,
since the verifier stands in the workspace and can read tiers 1–3.

The same rule governs a class outside the taxonomy: it is recorded as
`unparsed` with the raw token preserved in the prose, never mapped to the
nearest member. Mapping would manufacture agreement the session never
expressed.

## Rejected alternatives (steelmanned)

**A dedicated `summary_command` config key instead of `--tools ""`.** The
ticket named this as the other candidate mechanism, and its case is real: it is
explicit at the config layer, it cannot be broken by a CLI flag rename, and an
operator reading `project.env` would see exactly what the summary pass runs.
Rejected because it moves a correctness property — *this pass has no tools* —
out of the code and into per-project configuration that nothing validates. Every
project would have to get it right independently, a project that got it wrong
would run a fully-tooled "tool-less" pass silently, and the failure would look
identical to success. The flag-rename risk it protects against is the one this
buys back with a `--help` assertion that fails loudly at the flag.

**Let the summary pass fail closed — no report when inference is unavailable.**
Simpler, and avoids publishing a comment whose prose half is empty. Rejected
because the budget path is where the report matters most *and* where inference
is least available: a session that spent its cost ceiling cannot afford the turn
that would report its own death. Failing closed would mean the ceiling that
fires most often is the one that reports least. The mechanical fallback is
narrow on purpose — turns spent, cost vs ceiling, branch, commits ahead, last
event — but "narrow" beats "absent" for a verifier that has tiers 1–3 anyway.

**Resolve the two classes into one, preferring the self-report.** Yields a
single field consumers can route on without deciding anything, which is what
#12's dashboard and #31's router would each rather have. Rejected on the
methodology's own terms: conflict is evidence, not failure. Two careful readings
diverging is the finding, and the module with the least context is the wrong
place to collapse it.

**Post the report at the park / fail-review boundary instead of inside
`_worker`.** That boundary is where the routing decision already lives, so the
code would sit next to its consumer. Rejected because `session_id` is a live
local inside `_worker` and nothing survives to the park boundary but a reason
string and a workspace path — there would be no session to resume, which is the
one thing this feature needs.

## Blast radius

- **Purely additive at this boundary.** Both ceilings already `break` normally,
  so `_on_worker_done` takes the `exc is None` branch and logs
  `outcome=completed`. That is unchanged: `_post_cap_hit_report` catches
  everything, and a failure logs `cap-hit report failed; session ends
  unreported` and exits the same way. A raise here would convert a capped
  session into a FAILED one and re-enter retry/circuit accounting — turning a
  post-mortem into a routing change, which is #31's job.
- **No park-routing or episode-bound change.** Non-implement cap-hits and second
  implement cap-hits still fall through to `_park`
  (`test_fail_review.py:341`, `:417` unchanged).
- **One new field on `TurnResult` (`text`)**, populated only on the clean-success
  branch — the one place `result` is model-authored rather than CLI-authored
  error text (the #116 trust boundary, read from the other side). Every existing
  consumer ignores it.
- **Nothing crosses the runner→orchestrator boundary that did not already.** No
  tool-event retention, no denial counting, no repeated-command detection.
- **#12 gains a YAML block to parse**, and its scalars are JSON-quoted so a
  branch name containing a colon cannot take the dashboard's parse down.
- **Cost:** one extra provider turn per cap-hit, recorded and never compared
  against the ceiling it runs after.

## Weakest point

**The `--help` assertion pins the flag's documentation, not its behaviour.** A
CLI that kept the text and changed the semantics — or that honoured a
previously-appended `--allowedTools` over a later `--tools ""` — would ship a
fully-tooled pass while the test stayed green. Live verification needs a
`claude -p` probe, which is outside the worker allowlist, so this is assigned to
the human merge gate. That assignment is *exactly* the pattern the 2026-08-15
sweep flagged in AgDR-036: a record that hands verification to a gate and is
then merged without it. It is written here so the next sweep can catch it.

**What would make this wrong:** if the summary pass turns out to succeed on the
budget path more often than not — because the provider bills the resume against
a different ceiling than the orchestrator's — then the mechanical fallback is
carrying far less weight than this record assumes, and the honest simplification
is to drop the "expected failure" framing from the prose. Conversely, if it
*never* succeeds on either path, tier 4 stays empty in practice and the whole
feature reduces to a mechanical facts comment, at which point the tool-less
resume should be deleted rather than kept as decoration.
