# sb Guards + Quota/Liveness — Design (M0, Plan 3 sub-plan B)

**Status:** Approved design 2026-06-16; **revised 2026-06-17 against A's
implemented artifacts** — the three seams that deferred to A (the deny→blocked
contract §3/§9, per-task budget §3, the early-churn detector §6) are now
finalized. Graded depth: full — B is deterministic and the keystone for safe
autonomy. Independent of A for the hooks/quota/monitor; the early-churn slice
consumes A's real loop-ledger. Implementation plan to follow via writing-plans.

**Scope:** The safety + observability layer of the M0 judgment layer — the
deterministic tripwire guard (rabbit_guard v2), the token-free quota detector,
the external token-free monitor (liveness + quota + silent-session-death), and
soft subagent budget enforcement. All **token-free** (no model calls, no API
key). This is the layer that makes autonomous fleet execution safe (PHI-030);
the fleet cannot self-build until A+B exist and D validates them under human
supervision.

**Governing decisions/spec:** v2 design §6 (verification + guards), §7 (notify),
§8 (quota/failure taxonomy), §11 #1 (soft budget enforcement), §11 #3 (silent
session death), HDR-011 (deterministic detector + external token-free monitor;
quota advisory, never a claim gate), PHI-030 (verification before autonomy),
PHI-001 (background services session-independent — satisfied here because the
monitor makes NO model/API calls). HDR-009 (shared fleet throttle is
simultaneous — why quota must never gate a claim).

---

## 1. Verified hook mechanics (the foundation, confirmed against Claude Code docs)

- Hooks configured in the project `.claude/settings.json` **fire on subagent
  tool calls**, not just the top-level session. The payload carries **`agent_id`
  only when the hook fires inside a subagent** — this is the attribution key for
  per-subagent tripwire state (`agent_id` absent ⇒ the call is the worker
  session itself).
- **PostToolUse runs after the tool executed** — use it for *detection*
  (recording calls/errors/progress). It cannot prevent the next call.
- **PreToolUse runs before the tool** and can **deny** (exit 2 /
  `permissionDecision: "deny"`) — use it for the *blocking* second strike and for
  per-call budget enforcement.
- `SubagentStop` fires when a Task-tool subagent finishes, with
  `agent_transcript_path` (separate transcript) for post-mortem.
- Hooks fire recursively (nested subagents each get their own `agent_id`); hook
  scripts must use absolute paths or the payload `cwd` (path-resolution gotcha).

## 2. Components

- `hooks/sb_guard.py` — the tripwire guard (rabbit_guard v2), wired as **both**
  a PreToolUse and a PostToolUse hook (one script, dispatch on
  `hook_event_name`). The v1 paid Fable reviewer is **deleted**; its judgment
  role moved to the verification lane (§6).
- `hooks/sb_quota.py` — PostToolUse hook; token-free rate-limit detection →
  `.switchboard/quota.json`.
- **External monitor** — a token-free scheduled invocation of existing `sb`
  verbs (`sb status --emit` + `sb notify`), no new daemon. Surfaces quota +
  liveness + silent death + the early no-progress churn signal.
- Per-subagent guard state under `.switchboard/guard/<agent_id>.json`.

The `sb` engine is untouched (B adds hooks + a monitor wiring, not engine code),
except possibly tiny config keys for thresholds.

## 3. Tripwire guard (rabbit_guard v2)

**Detection (PostToolUse, keyed by `agent_id`).** Maintain a small per-agent
ledger: recent `(tool_name, hash(tool_input))` calls, recent error signatures, a
running tool-call count, and a **last-progress marker** (updated on a file write
under the worktree / a git commit / a result-file write). Tripwires:

- **repeat-call** — same `(tool, input-hash)` ≥3 times in the last 10 calls.
- **repeat-error** — same error signature ≥3 consecutive.
- **no-progress** — ≥15 tool calls since the last progress marker.
- **budget** — per-subagent tool-call count over `guard.max_tool_calls`
  (default 80) or wallclock over `guard.max_wall_s` (default 1200). **M0 uses
  these global config defaults only.** The task schema's per-task `budget` block
  (`tool_calls`/`wallclock_s`) is forward-looking metadata; wiring it through to
  the guard (the worker would publish the active task's budget where the hook
  reads it, keyed via cwd→worker_id) is **deferred past M0** — per-task ceilings
  matter more at fleet scale, and the schema field stays valid for later. So the
  schema comment "the numbers the PostToolUse guard enforces" describes the
  eventual state, not M0.

(Defaults are starting points; see §7 revision condition.)

**Two-strike action.**
- **1st trip** → PostToolUse injects a corrective nudge (exit 2 / stderr, shown
  to the agent as feedback) and marks the agent `tripped_once`.
- **2nd trip** → the next **PreToolUse** denies the offending tool and directs
  the subagent to stop. The guard does **detection + denial only** — it does not
  write engine result files (that would break the "result file is the worker's
  channel" layering).

**The deny→blocked contract (finalized against A; A side now built).** When a
guard-forced stop leaves the subagent returning with no result file, the
guarantee that this becomes a human-pausing `blocked` (not an error or a silent
re-loop) lives in **A**, now implemented: `sb block <id> --reason` synthesizes a
`blocked` result and routes the task to paused-for-human, and SKILL.md step 7
calls it when a task subagent returns no result (a crashed *verifier* instead
`release`s — infra, not human-blockable). This keeps outcome semantics in the
loop, needs no agent_id→task_id mapping in the hook, and is robust to a subagent
that ignores the deny directive. B itself only denies + nudges; it relies on
`sb block` existing (it does) for its integration test.

**Fail-open, always.** Any hook error, malformed payload, or missing state →
exit 0, never crash or stall a session (v1 discipline). Cooldown + a hard
per-agent nudge cap prevent nudge spam.

## 4. Quota detector (HDR-011)

`hooks/sb_quota.py` (PostToolUse): inspect `tool_response` for rate-limit /
429 / usage-cap signal strings (token-free regex; no model reasoning — works
even when the session has no tokens left). On match, write
`.switchboard/quota.json` `{state: "throttled"|"exhausted", detail,
retry_after_s, at}`. **Detection only.** The worker loop (A) reads it advisory
to tune backoff; nothing ever gates a claim on it (a shared throttle hits the
whole fleet at once, HDR-009 — a quota-gated claim plus a stale file would wedge
everyone). Absent ⇒ `{state: ok}` (digest default, already shipped in Plan 2).

## 5. External monitor (liveness + quota + silent death)

A **token-free** scheduled job (macOS `launchd` plist / cron) that runs the
existing `sb status --emit` then `sb notify` every M minutes. It reads
`.switchboard/` only and makes **no model/API calls**, so it keeps reporting
even when the entire fleet is capped or dead — covering quota state, stale
heartbeats (fleet stalled / silent session death, §11 #3), gates-ready, and
paused-for-human. No new long-running Python daemon (avoids the
session-scoped-daemon rate-limit-saturation failure that burned the
hindsight-embed daemon): it is just the scheduler + the CLI verbs already built
in Plan 2. Deliverable is a documented plist/cron entry + a thin wrapper if
needed.

## 6. Early no-progress churn detector (the one A's coarse cap defers to)

A's loop writes a token-free per-iteration ledger
`.switchboard/loop-ledger-<worker_id>.jsonl` (one JSON line per **task**
iteration: `{i, claimed_id, type, outcome, released, wall_s}`). Concrete facts
that pin this detector (confirmed against the shipped `sb/loopledger.py`):

- `outcome` is the lane `file-result` returned (`paused`/`done`/`queued`/
  `failed`) or the literal `released` (rate-limit infra-requeue). A task reaches
  `done` only via its verify-pass line, so **`outcome == "done"` is the progress
  marker.**
- **Idle waits are NOT logged** (A heartbeats and loops without a ledger line),
  so consecutive ledger lines are consecutive *task* iterations — an idle worker
  cannot trip this detector. This is exactly the false-positive A's idle-fix
  removed from its own checkpoint; B inherits the clean signal.

B's monitor **reads** that ledger and flags **N consecutive trailing lines with
`outcome != "done"`** (i.e. releases/retries/failed with no completion) → fires
an early notify, well before A's coarse `max_loop_iterations` checkpoint.
**Reuse `sb/loopledger.py` rather than re-parse:** it already owns the ledger
schema and the productive/churn aggregation (`diagnose`); add a small
`consecutive_no_progress(ledger_path) -> int` helper there (TDD'd alongside the
existing diagnose tests) and have the monitor call it. This lives in the monitor
(consuming A's ledger), not a hook — simplest place, no new in-session
machinery. `N` is a tunable default (see §7 revision condition).

## 7. Subagent budget enforcement (resolves the M0 research task)

Decision: **hooks, not loop checks.** The worker loop cannot see inside a
subagent's execution; the PreToolUse hook sees every subagent tool call with
`agent_id`. So per-subagent tool-call and wallclock budgets are enforced in the
guard hook (§3 budget tripwire): soft — nudge then deny, never a process kill
(§11 #1 accepts that a pathological subagent wastes some quota until the
tripwire or lease timeout catches it).

**Revision condition (thresholds):** all numeric defaults (repeat counts,
no-progress window, budgets) are first guesses. Tune from real `loop-diagnostic`
and guard-state data once A runs; if defaults prove noisy (false trips on
healthy work) or too loose (rabbit-trails slip through), adjust and record.

## 8. Testing (deterministic — genuinely TDD-able, unlike A)

- **Guard hook** — feed synthetic Pre/PostToolUse payloads (with and without
  `agent_id`); assert each tripwire fires at its threshold, 1st trip nudges, 2nd
  trip denies, malformed input fails open, cooldown caps nudges.
- **Quota hook** — feed `tool_response` with/without rate-limit strings; assert
  `quota.json` contents and the no-op-on-clean case.
- **Monitor** — seed a stale heartbeat / `quota.json` / churning loop-ledger in a
  temp `.switchboard/`; run the monitor command with a `null`/collector notify
  channel; assert the digest is emitted and the right events fire.

## 9. Scope / dependencies

- **Independent of A** for the hooks + quota + monitor. Two slices touch A's
  now-shipped artifacts: the *early churn detector* (§6) consumes A's loop-ledger
  and extends `sb/loopledger.py`; the *deny→blocked contract* (§3) depends on the
  A hardening follow-on (worker synthesizes `blocked` on a missing result file).
- **Deny→blocked contract — resolved and A side built**: the guard denies +
  nudges only; A's worker synthesizes the `blocked` result via `sb block` (§3).
  No hook writes engine result files. B's guard work can proceed in parallel;
  its integration test depends on `sb block`, which now exists.
- **Per-task budget — resolved:** M0 guard enforces global `guard.*` config
  defaults only; per-task `task.budget` wiring is deferred past M0 (§3).
- Does **not** include the HDR-010 escalation routing (that is sub-plan C) — B
  provides the notify *firing* and the deterministic guards; C decides tier and
  routing.
