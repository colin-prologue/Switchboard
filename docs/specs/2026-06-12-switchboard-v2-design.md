# Switchboard v2 — Platform-Native Design

**Status:** approved in design review 2026-06-12 (Colin + Claude session)
**Supersedes:** the v1 flat reference implementation's execution and coordination
mechanics (worker.py, bootstrap.py git-lock seeding, rabbit_guard.py API reviewer).
The mission (MISSION.md), the decision-spine concept, and the schema-contract
discipline carry forward unchanged.
**Decision records:** HDR-006, HDR-007, HDR-008, HDR-009 (in `decisions/`).

---

## 1. What changed and why

The v1 critical review found that most defects shared one root cause: a Python
harness re-implementing things Claude Code provides natively, and fighting the
platform where they overlapped. Concretely:

- **Git-as-lock contradicted gitignored state** (worker.py claim races vs
  HDR-003/ARCHITECTURE's transient `.switchboard/`). Both could not be true.
- **Shared working tree**: all workers and model sessions ran in one checkout;
  2+ concurrent workers would clobber each other's source edits.
- **Self-graded verification**: the session doing the work also judged done-ness
  for all non-`command` verify kinds — a direct PHI-030 violation.
- **Auto gates were never evaluated** by any component.
- **Blind retries**: requeued tasks re-prompted without the prior attempt's result.
- **Quota exhaustion burned tasks**: rate-limit failures were indistinguishable
  from task failures and exhausted attempts into the `failed` lane.
- **rabbit_guard required an API key** (API billing) and failed open without one —
  inert in the subscription-only deployment it was designed for.
- **`claude -p` moves to API-rate billing** (policy change imminent at design
  time), removing the v1 executor's economic basis.

The v2 answer: stop wrapping the platform; ride it. The engine becomes **file
contracts + skills + a small deterministic CLI**, and the runtime becomes
**ordinary interactive Claude Code sessions on subscription billing**.

A second reframe dissolved the apparent conflict between the project premise
("long-running agent sessions") and HDR-001 ("fresh session per task"):
HDR-001's real commitment is fresh **context** per task, not fresh OS process
per task. Subagent-per-task inside a long-running session satisfies it exactly
(and is PHI-012's own validated pattern).

## 2. Shape

Three layers, strictly separated:

1. **Contracts** — JSON Schemas for plan, task, result, decision record, status
   digest. Versioned, `additionalProperties: false`, actually enforced
   (jsonschema validation at every read/write boundary in the CLI).
2. **Deterministic engine** — `sb`, one small Python CLI: lane moves, leases,
   claims, DAG validation, ID allocation, seeding, brief assembly, digest
   emission, decision queries. Unit-testable, no model calls, no judgment.
3. **Judgment** — Claude Code skills (`/sb-work`, plus prompt protocols for
   task / verifier / planner subagents). All model invocation happens inside
   interactive sessions; **no `claude -p`, no direct API calls, no TUI
   automation anywhere in the system.**

Workers are started by hand on the dedicated machine (a tmux pane each, opened
by Colin, not driven by automation). The fleet scales by opening another
session. A wedged session is surfaced by the notification layer, not a watchdog
— accepted trade for zero TUI automation.

## 3. Execution model

### 3.1 The worker loop

A worker session runs `/sb-work`, a thin self-pacing loop that never performs
task work in its own context:

```
loop:
  sb claim --wait <secs>     # blocks inside the tool call until a task appears
  dispatch task subagent     # fresh context; model = tiers[task.tier]
  sb file-result <id>        # schema-validate, lane move, enqueue verification
  repeat / self-pace         # backoff on rate-limit signals
```

Properties:

- **Fresh context per task** — each task executes in a subagent. The main
  session context grows only by loop bookkeeping (~hundreds of tokens/task).
- **Tier lives on the dispatch, not the session.** Subagents take a model
  override; `tiers.json` maps tier → model id. Any worker session serves any
  tier. The fleet is N generalist sessions sized by desired parallelism.
  One session can run the entire pipeline alone (plan → execute → verify →
  brief) — this is the self-hosting MVP shape. (HDR-006; supersedes HDR-001's
  pool mechanics, preserves its intent.)
- **Planning is a task type**, not a special process. `sb seed --goal "..."`
  enqueues a plan task; a planner-tier subagent writes `plans/<id>.json` + an
  SDR; `sb seed --plan` expands it into queue tasks.
- **The loop is stateless.** All state lives on disk. Sessions are disposable:
  kill one anytime, start a fresh one, nothing is lost. Context growth costs
  quota, never correctness.
- **Heartbeat per iteration.** Each loop pass touches
  `.switchboard/heartbeats/<worker_id>`; the status digest flags a worker stale
  past the lease TTL, which drives the stalled-fleet notification (§7) and
  lease-expiry requeue (§4.1).

### 3.2 Task subagent protocol

The dispatch prompt carries: goal, done.statement + verify, constraints,
grounding (decision digests via `sb query`), prior-attempt result if this is a
retry or continuation (fixes v1's blind retries), and the AgDR protocol (§5).
The subagent works in a **git worktree** of the phase branch (§4.3), commits
its work there, and writes a result file against the result schema. The worker
validates and files it; it never parses freeform output (v1 discipline kept).

### 3.3 Research handoffs, continuations, and deadlock prevention

An AgDR is written only **after** the agent researches the decision. Research
within the agent's depth happens inline. Research needing a different agent
class becomes a spawned task — which creates dependent chains, governed by:

- **A waiting parent never holds a worker.** At a research handoff the subagent
  writes a `paused_for_research` result; `sb spawn` enqueues the research task;
  the parent re-enqueues as a *continuation task* depending on it. The
  continuation prompt carries the partial work plus the research output. The
  worker slot is released immediately.
- **Cycle check at enqueue.** `sb spawn` validates the dependency DAG and
  rejects any edge that creates a cycle (including ancestor dependencies).
- **Max chain depth 3.** Deeper recursion pauses for human — runaway
  decomposition is a spiral, not diligence.
- **Lease timeout + stale-fleet notification** backstop the degenerate cases
  (orphaned dependency, dead session).

With no held resources and an acyclic dependency graph, classic deadlock cannot
occur; the backstops cover livelock and abandonment.

### 3.4 Idle polling and context burn

`sb claim --wait <secs>` blocks inside the Bash tool call (filesystem watch in
Python) until a task appears or timeout. Waiting costs zero tokens; an empty
wait costs one tool call (~50 tokens of context). Idle burn ≈ 300 tokens/hour.
Combined with disposable sessions (§3.1), context burn is a cost concern, not a
correctness concern. **M0 research task:** tune wait duration against Bash tool
timeout ceilings; measure idle cost over a multi-day run; evaluate whether
self-pacing wakeups compose better than blocking claims.

## 4. Coordination substrate (HDR-007)

### 4.1 Queue

`.switchboard/` is **gitignored filesystem state**. Lanes are directories
(`queued/ active/ paused/ done/ failed/`); a claim is an atomic rename into
`active/` plus a lease file `{worker_id, claimed_at, ttl}`. Stale lease ⇒
requeue (never `failed`). Single-machine by design at this stage; a second
machine or cloud worker requires a sync layer to be designed when one actually
exists. The v1 git-lock machinery (claim commits, push races) is deleted.

### 4.2 Tracked vs transient — the explicit boundary

| Tracked (committed, travels with the code) | Transient (`.switchboard/`, gitignored) |
|---|---|
| `decisions/` — ALL records: ADR, AgDR, HDR, SDR | queue lanes, leases, heartbeats |
| `plans/` — plans are architectural documentation | in-flight results, prompts |
| review briefs — durably in the PR body | status digests |

**Evidence-durability invariant:** a decision record may only cite evidence
that survives `.switchboard/` cleanup. AgDRs land in `decisions/` *before*
their phase's PR opens; the brief embeds result summaries into the PR body.

### 4.3 Branch and worktree model

Each phase executes on a phase branch (`plan-001/ph-2`). Each task subagent
works in a fresh **worktree** of that branch and commits per task. Phase
completion opens a PR to main. Orchestration state never appears on any
branch. This eliminates v1's shared-working-tree failure by construction.

## 5. Oversight (HDR-008)

### 5.1 AgDR-instead-of-prompt

At any point where a session would normally request human input, the agent
instead: researches (inline or via spawned task, §3.3), writes an **AgDR**
using the cross-project ADR-043 template — including a **steelman of rejected
options** and a **blast-radius note** (PHI-028) — and proceeds on its best
judgment. **Hard-escalation domains remain true blockers** and pause for human
regardless: security boundaries, production deploys, secrets, frozen contracts
(PHI-028's exclusion list). `halt` is reserved for those plus genuine inability
to proceed.

### 5.2 Gates

- **The PR is the hard gate.** Every plan ends at one. The PR body is the
  review brief: goal, work delivered, verification verdicts, and the rich
  review profile — every AgDR from the phase with confidence, alternatives,
  and blast radius. Merge = ratification, binding (PHI-028). Feedback lands on
  the records via `sb stamp`; AgDR verdict history accumulates the
  earned-autonomy track record.
- **Optional Spec→Plan gate.** A plan flagged `gate: plan` (or carrying
  blocking open questions) holds before seeding until `sb plan approve`. Used
  where decisions compound; the only pre-execution block.

### 5.3 Planner-defined process weight

The planner chooses the process shape and justifies it in the SDR:

- `full` — design phase + Spec→Plan gate + implementation phases + PR gate
- `standard` — phases + PR gate
- `patch` — single phase, straight to PR (bug fixes, small-medium tasks)

Human may override at seed time. **Two invariants are not plannable:** every
plan ends at a PR gate, and every task passes verification. Watch item: planner
under-weighting to dodge overhead; the SDR rationale plus PR-time feedback is
the tuning loop.

## 6. Verification and guards (PHI-030)

Task completion enqueues a **verification task**. The claiming session
dispatches a verifier subagent — **different model than the author, fresh
context** — which runs the machine-checkable `verify` (command/test) and judges
the diff against `done.statement`. Only a verifier verdict moves work to
`done`; failure reopens the task with the prior result in the retry prompt.
`verify.kind` must be machine-checkable for autonomous flow; `review`-kind
routes to the PR profile. Verification exists in M0, before any autonomy —
PHI-030 is a pre-commitment, not a milestone.

**rabbit_guard v2:** deterministic tripwires (repeat-call, repeat-error,
no-progress, budgets) as Claude Code hooks — free, no model calls, no API key.
First trip injects a corrective nudge; second trip forces a `blocked` result
and pauses the task for the verifier/human. The v1 paid fresh-reviewer API call
is deleted; its judgment role moves to the verification lane, asynchronously.

## 7. Notification (PHI-029)

The medium ships in M0, not with the nexus. `sb status --emit` produces the
status digest (also the future nexus read-side primitive). A notify hook fires
on: PR/gate ready, task paused for human, fleet stalled or quota-exhausted.
Channel pluggable: macOS notification by default; ntfy/Teams later. Review
never requires remembering to check a terminal.

## 8. Quota and the failure taxonomy

Infra-failure ≠ task-failure, structurally:

- Rate-limit/usage-cap signals from a dispatch are **infra failures**: the loop
  backs off (self-pace longer), the task returns to `queued` with attempts
  **unchanged**, and the digest flags quota state.
- Only a verifier rejection or attempt exhaustion (default 3) produces
  `failed`.
- Tier routing remains the quota lever: top tier reserved for compounding
  decisions (unchanged principle, quota-denominated economics).

**Accepted risk (HDR-009):** fleet-scale automated use of subscription sessions
is mechanically identical to a human running several terminals, but sits in a
policy gray zone as headless billing changes land. Fallback if policy tightens:
reduce concurrent sessions; route overflow through API billing consciously.

## 9. Component inventory

| v1 | v2 fate |
|---|---|
| worker.py | dissolves into `/sb-work` skill + `sb` CLI |
| bootstrap.py | becomes `sb seed`; planning becomes a task type |
| gate.py | becomes `sb brief` / `sb stamp`, PR-oriented |
| rabbit_guard.py | rewritten as deterministic hooks; API reviewer deleted |
| query_decisions.py | survives as `sb query` (keyword now; embeddings later) |
| schemas/ | survive; gain result/digest schemas, AgDR alignment (ADR-043 fields: steelman, blast radius), continuation/spawn fields; jsonschema enforcement added |
| tiers.json | survives unchanged in role |
| git-lock claim/push code | deleted |
| `.decisions/` (demo) | demo artifacts removed; real records live in top-level `decisions/` |

## 10. MVP scope and the self-hosting bar

**M0 — hand-built (the only manual milestone):**
`sb` CLI (lanes, leases, claims with `--wait`, DAG-checked spawn, jsonschema
validation, seed, brief, digest, stamp, query), `/sb-work` skill with subagent
dispatch + quota backoff, task/verifier/planner subagent prompt protocols,
verification lane, notify hook, schema updates, worktree/branch mechanics.
Research tasks inside M0: idle-poll tuning (§3.4); subagent budget enforcement
mechanics (hooks vs loop checks).

**M0 exit bar:** a 2-phase toy plan runs end-to-end on the dedicated machine —
plan task → exec tasks in worktrees → verification → PR with AgDR profile →
Colin's merge unblocks phase 2 — with at least one research-handoff
continuation chain exercised.

**M1 — first dogfood:** PLAN-001 = the system builds its own remaining engine
(AgDR tooling polish, digest/brief improvements, packaging as installable
plugin/CLI per ARCHITECTURE.md's target topology). Every defect found is
captured as a decision record.

**Deferred:** nexus (digest primitive ships in M0), embeddings retrieval,
multi-machine substrate, rich visual decision view.

## 11. Known weakest points

1. **Soft budget enforcement** — subagent wallclock/tool budgets are enforced
   by hooks and loop checks, not process kills. A pathological subagent wastes
   quota until a tripwire or lease timeout catches it.
2. **AgDR discipline is the trust-the-model link.** The rich review profile is
   only as good as in-flight record-keeping; schema enforcement and verifier
   spot-checks mitigate, not eliminate.
3. **Silent session death** — self-paced loops die without a supervisor;
   detection latency = stale-heartbeat notification interval.
4. **Policy exposure** — §8's accepted risk; revisit when the billing change
   ships.
5. **Single human remains the ratification bottleneck at scale** — mitigated by
   async PR gates and process weights, unresolved by design until the nexus.
