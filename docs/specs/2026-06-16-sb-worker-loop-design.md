# sb Worker Loop + Subagent Protocols — Design (M0, Plan 3 sub-plan A)

**Status:** Approved 2026-06-16. Implementation plan to follow via writing-plans.

**Scope:** The execution spine of the M0 judgment layer — the `/sb-work`
worker loop and the **task** and **verifier** subagent prompt protocols, plus
the one engine verb the loop needs (`sb release`). This is sub-plan **A** of the
Plan 3 decomposition (A worker loop · B guards+quota · C escalation · D exit
bar). It is deliberately the lean spine: planner protocol and research-handoff
continuations are split out (see §7).

**Governing decisions/spec:** HDR-006 (subagent-per-task inside long-running
interactive sessions; no `claude -p`), HDR-007 (file-queue substrate), HDR-009
(subscription billing, interactive sessions), PHI-030 (independent verification
before autonomy), PHI-033 (explicit worktree isolation), v2 design §3.1–3.4
(worker loop, subagent protocol, handoffs, idle poll), §4.2–4.3 (tracked vs
transient; branch/worktree model), §6 (verification), §8 (quota/failure
taxonomy). HDR-012 keeps the deliberation front-end out of scope here.

---

## 1. What this builds

Long-running interactive terminal sessions — one per model/type, hand-started
(HDR-006/009) — each running `/sb-work`. The loop never performs task work in
its own context; it claims a task, provisions an isolated git worktree,
dispatches a fresh-context subagent at the task's tier, files the validated
result, and tears the worktree down. The main session grows only by loop
bookkeeping (~hundreds of tokens/task, spec §3.1).

Components:

- **`/sb-work` skill** — `SKILL.md` + `task-protocol.md` + `verifier-protocol.md`,
  in the repo's reviewed/canonical skills directory (not user-level — avoids the
  skill-drift divergence trap).
- **`sb release <id>`** — new engine verb: move a task `active → queued` with
  **attempts unchanged** and the lease dropped. The only Python/TDD-able unit in
  A. Needed because the existing `file-result` requeue path increments attempts
  (correct for task failures) and there is no infra-failure path today, while
  §8 requires a rate-limited dispatch to requeue attempts-unchanged.
- Consumes (already built): `sb claim --wait`, `sb heartbeat`,
  `sb file-result`, `sb query`, `tiers.json`, `git worktree`.

The `sb` engine remains git-free; the skill owns all git operations.

## 2. The loop

```
worker_id = stable per-session id
loop:
  sb claim --wait W --worker-id ID            # blocks in-tool; exit 3 = nothing
    exit 3 → sb heartbeat; if quota.json advises throttled, lengthen W; repeat
  task T (JSON on stdout) → sb heartbeat
  branch = "<plan_id>/<phase_id>".lower()
  ensure branch exists (create off integration base on the phase's first task)
  git worktree add WT branch
  model = tiers[T.tier]                        # tier lives on the dispatch
  dispatch subagent (Agent tool, model override, work in WT):
      verifier-protocol  if T.context.verifies   else  task-protocol
  subagent writes .switchboard/results/<id>.json and commits work to branch
  sb file-result <id>                          # validate, lane move, enqueue verify
    dispatch raised a rate-limit/usage-cap signal → sb release <id>; back off; repeat
  git worktree remove WT                        # branch + commits persist
  repeat
```

Properties: stateless (all state on disk; sessions disposable = killable
without loss, not short-lived); heartbeat every pass feeds the stale-fleet
signal (§7, sub-plan B); fresh context per task; any worker serves any tier.

## 3. Subagent protocols

### Task protocol
The dispatch prompt carries: goal; `done.statement` + `done.verify`;
constraints; grounding (decision digests via `sb query`); **the prior result
when this is a retry or continuation** (fixes v1's blind retries); the
AgDR-instead-of-prompt protocol (ADR-043 template — steelman of rejected
options + blast-radius note, PHI-028); and the worktree CWD. The subagent does
the work, commits to the phase branch, and writes a result file against the
result schema. **Hard-escalation domains** (security boundaries, production
deploys, secrets, frozen contracts) remain true blockers: the subagent files a
`blocked` result rather than proceeding. The loop never parses freeform output
(Plan 1 discipline) — the result file is the only channel.

### Verifier protocol
A **different model than the author, fresh context** (PHI-030). Runs the
machine-checkable `done.verify` (command/test) and judges the committed diff
against `done.statement`, writing a result with `verdict: pass | fail` and
`verdict_notes`. Only a verifier `pass` moves work to `done` (enforced by the
existing `file-result` routing). A `fail` reopens the task with the prior
result in the retry prompt.

## 4. Worktree lifecycle (PHI-033)

The skill provisions the worktree **before** dispatch and removes it **after**
filing the result — isolation is guaranteed before any task code runs, not
hoped for. The phase branch (`<plan>/<phase>` lowercased) is created on the
phase's first task off the integration base. Commits live on the branch, so
teardown is safe even if a subagent left the worktree dirty (the result file
lives in `.switchboard/`, not the worktree). This eliminates v1's
shared-working-tree failure by construction (§4.3).

## 5. Infra vs task failure (§8)

- A **rate-limit / usage-cap signal on dispatch is an infra failure**: the loop
  calls `sb release <id>` (→ queued, attempts unchanged), backs off (lengthens
  the wait), and continues. The reactive 429 path lives here; the *token-free
  detector* that writes `quota.json` is sub-plan B. A reads `quota.json`
  advisory-only to tune backoff and never gates a claim on it (HDR-011).
- A **verifier rejection or attempt exhaustion** (default 3) is the only path to
  `failed` (existing `file-result` behavior).

## 6. Testing strategy (honest about a skill's limits)

Unlike Plans 1–2, A is mostly a skill + prompts, which are not pytest-able.

- **`sb release`** and any engine touch → full TDD, lane-transition and
  attempts-unchanged assertions, like the rest of the engine.
- **Loop mechanics** → an integration test with a **stub dispatcher**: a script
  that writes a canned result file instead of calling a model. Seed a toy task,
  run claim → stub → file-result → lane move, and assert lane transitions and
  worktree create/remove. Deterministic, no model calls. (Post-hoc assertion
  pattern: the interactive entry point is exercised by a harness that stands in
  for the model.)
- **Prompt protocols** → not unit-testable. Reviewed against this spec, then
  **exercised live in the exit bar (D)**. This is the lowest-confidence part of
  the milestone and is named as such: A's prompts get review + D, not a green
  test; the mechanics around them get tests.

## 7. Scope lines (deferred deliberately)

- **Planner protocol + `sb seed --goal`** → `A-planner`, landed before D (D is
  the first thing needing goal→plan). The loop dispatches a planner identically;
  only the entry point and SDR/plan emission are new.
- **Research-handoff / continuation** (`paused_for_research` → `sb spawn` →
  continuation task depending on it, §3.3) → its own follow-on before D, which
  requires exercising one continuation chain. `sb spawn` exists; this needs a
  result-outcome + re-enqueue, so it is real engine work, not A's spine.
- **Tripwire guards + token-free quota detection** → sub-plan B.

## 8. Accepted risk + revision condition

A has **no hard iteration/budget cap** in the loop (the lean choice). Drift
containment leans on fresh-context subagents and the sub-plan B external
monitor (liveness) + tripwire hooks (runaway). **Revision condition:** if a
long-running session is observed to drift, loop, or wedge in practice before B's
monitor catches it, add a hard iteration/budget cap to the skill (the bounded
self-terminate + external-restart variant). Recorded here so the omission is a
conscious, reversible choice rather than an oversight.
