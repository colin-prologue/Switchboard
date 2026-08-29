# AgDR-2026-08-29 — The Codex budget ceiling is wired end-to-end and inert at the source; the residual is named, logged, and pinned by a test

**Status:** proposed (ratify or overturn at the #181 merge gate)
**Issue:** #181 — Codex sessions have no budget ceiling: `CodexConfig` lacks
`max_budget_usd` and `CodexRunner` hard-codes `None`
**Supersedes / amends:** amends nothing. It discharges the reachability
condition #181's AC3 attached to itself, and records the residual that #135's
2026-08-28 audit predicted would appear here — the Codex adapter declining to
populate a provider-neutral interface.

## Context

`scheduler.py` enforces one per-run cost ceiling for every provider: read
`runner.max_budget_usd`, accumulate `TurnResult.cost_usd` across a session's
turns, end the session normally when the sum reaches the ceiling. That is the
AgDR-018 shape — policy is metadata the scheduler reads off whatever runner it
selected, so a new provider costs the scheduler nothing.

`CodexRunner` hard-coded `self.max_budget_usd = None`, and `CodexConfig` had no
field to read even if it had wanted to. A Codex session's only bound was
`agent.max_turns` (plus the per-issue session cap).

Fixing the two named layers is mechanical. The question this record exists for
is the third one #181 refused to let pass unexamined: **does the ceiling become
reachable once the config exists?**

It does not, and the reason is upstream of this repository. `codex exec --json`
emits `turn.completed` with a `usage` object — `input_tokens`,
`cached_input_tokens`, `output_tokens`, `reasoning_output_tokens` — and **no
dollar figure**, because the pilot runs on subscription authentication where no
per-run marginal USD cost exists to report. SPEC §1 already said this
("Codex reports no dollar budget in subscription mode"); what it did not say was
the consequence: `CodexRunner` normalizes every successful turn to
`cost_usd: 0.0`, so `cumulative_cost` stays at `0.0` forever and
`scheduler.py`'s ceiling check can never be true.

So the honest description of what shipped is: **the ceiling is wired at every
layer this repository owns, and inert at the one layer it does not.**

## Decision

### 1. Ship the wiring, and refuse to let it read as enforcement

`CodexConfig.max_budget_usd` exists, `_parse_codex` parses it under the Claude
envelope's strictness (numeric-or-null, boolean rejected), `CodexRunner` reads
it from config, and `WORKFLOW.pilot-codex.md` sets `5`. The scheduler path is
provider-neutral and is now covered by a Codex-routed integration test that
crosses a ceiling and ends the session the same way its Claude twin does.

A knob that silently does nothing is worse than no knob: it is a cap the
operator believes in. So the inertness is made loud in three places that a
reader cannot miss:

- **A log line at construction.** `CodexRunner.__init__` emits
  `codex budget ceiling configured but codex reports no cost telemetry;
  ceiling cannot fire` whenever a ceiling is set. Once per session, on the
  operator's stderr, next to the dispatch logs.
- **A comment where the value is typed.** Both the dataclass field and the
  pilot workflow's `max_budget_usd: 5` say what it does not do and point here.
- **SPEC §1 states the consequence**, not just the fact.

### 2. The residual is pinned by a test that fails when it closes

`test_configured_ceiling_does_not_make_codex_report_cost` runs a real fixture
Codex stream under a configured ceiling and asserts `cost_usd == 0.0` alongside
non-zero token usage. It is a characterization test: it documents the gap at
the exact seam where it lives, and the day Codex starts reporting a cost it
goes red and names its own remedy. This is the "artifacts that fail loudly"
requirement from the methodology's conflict-is-evidence section — a residual
recorded only in prose is a residual nobody re-reads.

### 3. The mechanism that closes it is one change, named in advance

When `codex exec --json` reports a per-turn or cumulative dollar cost (usage-
based auth, or a future CLI field), read it into `TurnResult.cost_usd` at the
`turn.completed` branch of `codex_runner.py`. Nothing else moves: config,
runner attribute, scheduler check, and pilot ceiling are already in place and
tested. **The field name is deliberately not guessed today** — see the rejected
alternatives.

## Rejected alternatives (steelmanned)

**Parse a plausible cost field now, so the path is complete.**
The strongest version: four lines reading `usage.total_cost_usd` (or
`turn.completed.cost`) with a `0.0` fallback costs nothing and closes the gap
the instant the CLI emits it. Rejected because the field name is a guess, and a
guess with a passing test is exactly the *fake fidelity* failure class the
methodology names: the test would prove our parser reads the key we invented,
the system would still report `0.0`, and the residual would now be invisible
because it looks handled. A named one-line remedy in a record is more honest
than a speculative parse dressed as coverage.

**Derive dollars from token usage times a price table.**
This would make the ceiling genuinely fire. Rejected on two grounds. It invents
numbers — a price table in config that nobody can validate and that drifts
silently against real pricing. And under subscription auth it would be
*counterfactual* spend: it caps a session on money that was never charged,
which is a different policy than "stop before you spend $5", adopted without
anyone deciding to adopt it.

**Add a token ceiling instead — the bound Codex telemetry actually supports.**
The most defensible alternative, and the likely right answer eventually: usage
tokens are reported, so `max_tokens` would be enforceable today. Rejected as
out of scope here. It is a new provider-neutral field on the runner contract
plus a second scheduler check plus a decision about how it interacts with the
dollar ceiling for Claude — a ticket, not a rider on this one. #181's AC3
explicitly permits the residual route, and taking it keeps this change to the
shape the issue specified.

**Refuse the config field until it can be enforced.**
Ship nothing rather than a dead knob. Rejected because it leaves the adapter
gap #135 named exactly where it was, and because the wiring is what makes the
closing change one line instead of five. The dead-knob objection is real; it is
answered by making the knob announce itself rather than by withholding it.

## Blast radius

- `CodexConfig` gains an optional field defaulting to `None`; every existing
  workflow parses unchanged.
- `_parse_codex` accepts one more key. A workflow that previously failed with
  `workflow_parse_error` on an unknown `max_budget_usd` now parses — a
  loosening, and the only behaviour change for existing configs.
- `CodexRunner.max_budget_usd` is no longer constant `None`. The scheduler
  reads it, but with `cost_usd` pinned at `0.0` no session ends differently
  than before. The mixed-mode Codex leg is affected identically.
- `WORKFLOW.pilot-codex.md` now emits one extra log line per Codex session.
- SPEC §1's Stage 5A field list and runner-contract paragraph change; no
  methodology change.

## Weakest point

**The log line is the whole anti-placebo mechanism, and log lines are the
thing operators stop reading.** If this ceiling is still inert in six months,
the most likely failure is not that someone was misled by the config — it is
that `max_budget_usd: 5` sits in the pilot workflow, gets copied into the next
project's binding by someone reading it as a working example, and the warning
scrolls past in a startup log nobody tails. A guard that depends on a human
noticing prose is the same shape as the residuals the 2026-08-15 sweep found
already fired.

The stronger form would refuse the config outright — raise at parse time when a
Codex block sets a ceiling the adapter cannot enforce — and that option is
genuinely arguable against what shipped. It was not taken because #181's AC5
asks the pilot to *set* the ceiling so the path is exercised, and a parse error
would make that criterion unsatisfiable. If the operator would rather have the
refusal than the pilot's example value, that is a one-line change and this
record is the place to overturn.

Second weakest: the reachability claim rests on Codex's current subscription-
mode output. If a Codex build reports cost under some auth mode not exercised
here, the residual is narrower than stated — and the pinned test is what would
surface that, by going red.
