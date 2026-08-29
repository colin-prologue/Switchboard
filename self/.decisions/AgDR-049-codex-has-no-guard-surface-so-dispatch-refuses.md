# AgDR-049 — Codex has no guard surface, so dispatch refuses the one stance that needs it

- **Status:** proposed (ratify or overturn at the merge gate)
- **Issue:** #135 (Codex-side merge guard; the residual `AgDR-036` named at
  filing and conceded again at `AgDR-043`)
- **Amends:** `AgDR-036` (adds the dated Codex-residual section; the enumeration
  and the Claude mechanism are untouched)
- **Touches:** `orchestrator/src/orchestrator/runner_selector.py` (the refusal
  and the single Codex construction point), `orchestrator/src/orchestrator/scheduler.py`
  (catch the refusal base, not one subclass), `orchestrator/src/orchestrator/codex_runner.py`
  (docstring naming the absent surface and where the compensating control lives),
  `orchestrator/tests/test_runner_selector.py`, `orchestrator/tests/test_merge_guard.py`

## Context

`#133` gave Gate C a mechanism for Claude-CLI sessions: `runner.py` materializes
`guard.py` into a settings file every turn and passes it via `--settings`, and
`#158` verified against the real CLI that a PreToolUse deny beats
`--allowedTools`. `CodexRunner` does none of this — `_build_argv` emits no
settings, hook, or guard flag on either the fresh or the resumed path — so a
Codex-routed session denies **none** of the enumerated shapes. The asymmetry is
confirmed rather than symmetric-unknown: one provider has a verified guard, the
other has no surface to hang one on.

Codex stays reachable on a real board regardless of today's `codex: 0` weight,
because label precedence overrides weights: a persisted `provider:codex` or an
operator `agent:codex` label routes there directly.

`AgDR-048` is why this stopped being tolerable as a named residual. In a
single-operator system no second human reviews a merge, so Gate C and the merge
guard are single points of failure — and that record names this exact asymmetry
as *more* urgent, not less. A residual in a record nobody re-reads is not a
floor; a mechanism is.

## The vendor question, and why the decision does not rest on it

Issue #135's first criterion was to establish Codex-CLI's PreToolUse equivalent.
It is **not determinable from inside a worker session**: `codex` is not
installed in the worker image and is on no worker allowlist; web search/fetch
are not granted; and the project's own record of the Codex config surface
(`SPEC.core.md` §5.3.6) enumerates `approval_policy`, `thread_sandbox`, and
`turn_sandbox_policy` — approval and sandbox *posture*, not a per-call veto. The
one command that would settle it, `codex app-server generate-json-schema`, sits
outside the allowlist. Details in the `AgDR-036` addendum.

That is precisely the shape `#133` mishandled: a vendor premise checkable only
at a gate. The failure there was not the deferral — it was shipping a mechanism
**whose correctness depended on** the unverified premise, with a green suite
that would have passed identically had the hook never been consulted.

So this record deliberately picks a mechanism that is independent of the answer.
The refusal is entirely orchestrator-side. It is correct if Codex has no hook
surface, and it is a conservative over-block if Codex turns out to have one. No
verification is assigned to the merge gate, because an assignment reads as
diligence and produces nothing.

## Decision

1. **One construction point for `CodexRunner`.** `runner_selector._codex_runner`
   is the only place the adapter is built. `CodexOnlyRunnerSelector` and
   `MixedRunnerSelector` both go through it, so a future selector cannot reach
   Codex by forgetting a check that lives in two `if` branches.

2. **Refuse Codex when the project's stance hands Gate C to an agent.**
   `_codex_runner` raises `CodexGuardUnavailable` when `_gate_c_repo(cfg)` is
   non-empty. That predicate is already the guard's own input on the Claude
   path, so the refusal and the guard can never hold different beliefs about who
   owns a project's Gate C. The message names the repo — an operator reading one
   log line has to be able to tell which board stopped moving.

3. **The refusal is at the selector, not at config load.** Weights alone cannot
   express reachability: a project with `codex: 0` still routes to Codex the
   moment an operator applies `agent:codex`. Only a per-issue check sees that.

4. **Scheduler catches a base class, not a subclass.** `AssignmentRefused` is
   introduced as the parent of both `MixedAssignmentRefused` and
   `CodexGuardUnavailable`, and `_dispatch` catches the base. Handling is
   identical and already correct: no claim, no label writes, no worker, one log
   line carrying `failure_class=assignment_refused`.

5. **The human gate is deliberately NOT refused.** On `base`, `gh pr merge` is
   denied outright for Claude and ungated for Codex — a real gap, and it stays
   open. The distinction is what the guard is *doing* in each case. On a human
   gate the guard restates a prohibition the prompt already carries and the
   handoff contract already enforces (the orchestrator, not the worker, performs
   the terminal transition). On an agent-owned gate the guard is doing something
   the prompt cannot: it **bounds** an otherwise-real merge right to the
   granting project's own repository, because one App installation token also
   reaches the human-gated repos in that installation. Refusing there prevents a
   cross-stance escalation. Refusing everywhere would disable a shipped provider
   outright, which is a bigger decision than this ticket, taken as a side effect.
   Asserted as a choice in `test_merge_guard.py`, not left implicit.

## Rejected options, steelmanned

- **Implement a real Codex-side guard.** The correct end state, and the branch
  `#135` names first. Rejected because the mechanism cannot be identified, let
  alone verified, from a worker session — and `AgDR-036`'s own history is the
  argument against shipping a guard built on a guessed vendor premise. Building
  one blind would most likely produce a settings file Codex silently ignores:
  a green suite, a fresh record, and no guard. Revisit the moment the surface is
  confirmed; decision 1 is where the change lands.
- **Refuse Codex entirely until it has a guard.** Strictly safer, and honest
  about the gap. Rejected as out of proportion and out of scope: it retires the
  Stage 5 canary (`--provider codex`, `SPEC.md`) and the whole mixed-routing
  path as a side effect of a Gate-C ticket. If the operator wants that, it
  should be that decision, argued on its own.
- **Fall back to Claude instead of refusing.** Keeps the board moving. Rejected
  because it silently overrides a *durable* `provider:codex` assignment, which
  exists specifically so retries are stable, and it converts an operator's
  explicit routing instruction into a no-op with no signal. A stall is visible
  in the log; a silent substitution is not.
- **Validate at workflow load: reject a config that is both agent-gated and
  codex-reachable.** Loud, immediate, once per boot — genuinely better where it
  applies. Rejected as insufficient alone (decision 3: labels defeat weights) and
  redundant on top of the selector check. Worth adding if the stall below proves
  to be the real cost.
- **Give the refusal a guarded issue comment** (like `_refuse_missing_marker`).
  Rejected for now as scope: it duplicates machinery for a path that fires only
  on a misconfiguration. Named as the weakest point instead, which is the
  honest place for it.

## Blast radius

- **No behaviour change for the only mixed-routing binding in this repo.**
  `projects/switchboard-self/WORKFLOW.pilot-codex.md` is the one workflow that
  can select Codex at all, and it declares no `handoff_label`, so it defaults to
  `status:human-review` and its `active_states` (`todo`, `in progress`) do not
  contain it: `agent_owns_gate_c()` is False, `_gate_c_repo` is empty, and
  `_codex_runner` returns the adapter unchanged. `test_merge_guard.py` already
  pins the two shipped stances (`base` human-gated, `prototype` agent-gated)
  against the real templates, so a stance edit that flips this fails there.
  Bindings outside this repo (`civ-life`) are not inspectable from a workspace
  clone; the rule that applies to them is stated in the next bullet, not assumed
  away.
- **The combination that now refuses** is agent-owned Gate C plus any route to
  Codex (label or weight). Those issues stop dispatching until the label is
  changed or the project's handoff moves back to the human gate.
- **`MixedAssignmentRefused` keeps its name and its behaviour** — existing tests
  and any external `except` clause naming it are unaffected. The scheduler's log
  string changes from "mixed assignment refused" to "provider assignment
  refused"; nothing asserts on it.
- **`test_merge_guard.py` gains its first Codex coverage.** One test pins the
  absent surface (the premise), one pins the refusal beside the Claude path it
  compensates for, and the human-gate residual is asserted as deliberate.

## Weakest point

**A refused issue stalls silently.** The refusal leaves the issue untouched and
logs one line per poll tick; nothing lands on the board. If a project moves to an
agent-owned stance while carrying `provider:codex` labels — or turns up
`codex` weight there — those tickets simply stop moving, and the operator's first
signal is a card that never advances. In a single-operator system (`AgDR-048`)
that is the expensive failure mode, and it is the same shape as the memory this
project already carries about guards that lapse without erroring. This inherits
the behaviour ambiguous provider labels have had since Stage 6 rather than
introducing it, but it widens the set of ways to reach it. The falsification
condition is concrete: **the first time a ticket stalls here and is noticed by
its card rather than its log, this decision was wrong to skip the guarded
comment**, and the fix is the rejected option above.

Second: decision 5 leaves the human-gate case open on purpose. The reasoning —
that the guard there restates a prohibition already carried by the prompt and
already enforced by the orchestrator-owned handoff — is a claim about the value
of the Claude guard, and it is in tension with `#133` having been filed at all.
If a Codex session on a human-gated project is ever observed merging its own PR,
that argument is falsified and the refusal should widen to every stance.
