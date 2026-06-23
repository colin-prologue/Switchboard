# sb Research-Handoff Continuation Implementation Plan (M0, Plan 3 sub-plan A-continuation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the research-handoff continuation chain end to end — let a task subagent signal "I need a different agent class to research X first," have the engine spawn that research and re-enqueue the parent as a continuation depending on it, and surface the completed research findings into the continuation's dispatch — per the worker-loop design §3.3.

**Architecture:** The continuation *engine* already exists and is tested (`sb/spawn.py::spawn_research` does the research-task creation, parent re-enqueue, partial carry-forward, DAG cycle-check, and chain-depth cap). This plan closes the integration gaps around it: (1) a `paused_for_research` result outcome + `research` request block so the subagent can signal a handoff through the result file (the only channel); (2) a `file-result` branch that delegates that outcome to `spawn_research` (engine-atomic — one validated door, ADR-005); (3) a small `sb result <id>` read verb so the worker can fetch a completed research task's findings (ADR-006); (4) the task-protocol "research handoff" section + the worker's continuation-dispatch logic. Adding the `research` field bumps the result schema `0.1.0 → 0.2.0` per the project's new-fields-version-bump convention.

**Tech Stack:** Python 3.11+, `jsonschema`, `pytest`. No new dependencies.

**Decisions recorded this round (both director-confirmed before planning):** **ADR-005** — `file-result` handles `paused_for_research` by delegating to `spawn_research` (engine-atomic), not the worker orchestrating a separate `sb spawn`. **ADR-006** — research output reaches the continuation via worker-fetch through a new `sb result <id>` read verb, keeping the engine write-path untouched.

---

## Background the implementer needs

Existing, tested machinery you build on (do not reinvent — import it):

- `sb/spawn.py::spawn_research(lay, cfg, parent_id, goal, tier, done_statement) -> research_task | None` — **already does the whole continuation dance**: validates the parent is `active`; bumps `chain_depth`; on depth > `max_chain_depth` (default 3) moves the parent to `paused`/`paused_for_human` and returns `None`; else creates a research task `"<parent>.R<n>"` in `queued`, runs `dag.assert_addition_ok` (cycle check on the parent→research edge), consumes the parent's partial result file at `lay.results/fname(parent)` into `parent.context.prior_attempts` (corrupt → a `{note:...}` placeholder; file removed either way), appends the research id to `parent.context.depends_on`, and re-enqueues the parent to `queued` (status `queued`, claim + result dropped, lease cleared, write-before-move). Covered by `tests/test_spawn.py`.
- `sb/results.py::file_result(lay, cfg, task_id) -> dest_lane` — the only door results enter through. Reads `lay.results/fname(id)`, validates against the result schema, embeds into the task, routes by outcome (success → `paused`/awaiting_verification + verify enqueued; blocked → `paused`/paused_for_human; partial/failed → requeue-or-fail) or applies a verdict for verify tasks. Write-before-move + clear-lease + `os.remove(rp)` at the end.
- `sb/store.py` — `find_task(lay, id) -> (lane|None, task|None)`, `read_json`, `write_json`, `fname`.
- `sb/cli.py` — argparse CLI; `_out(obj)` prints JSON; subcommand handlers follow the `if a.cmd == "...":` pattern. `spawn` already exists as a verb.

**Why `file-result` delegating to `spawn_research` is safe (ADR-005):** `spawn_research` reads and **consumes** the parent's result file itself (carrying it into `prior_attempts`), re-enqueues the parent, and clears the lease — i.e. it does the full lane transition. So `file-result`'s `paused_for_research` branch must call `spawn_research` and **return immediately**, skipping the normal embed/move/`os.remove` (spawn already removed the file and moved the task). No circular import: `spawn` imports `dag/leases/store/validate`; `results` will add `from sb import spawn` and `spawn` does not import `results`.

**Result schema today** (`schemas/result.schema.json`, v0.1.0): `required: [schema_version, outcome, summary]`; `outcome ∈ {success, partial, blocked, failed}`; `additionalProperties: false`; other optional fields `evidence, decisions_emitted, unblocks, verdict, verdict_notes, completed_at`. **You bump it to 0.2.0** and add `paused_for_research` + a `research` block.

**Every place that writes a result hardcodes `"schema_version": "0.1.0"`** — bumping the const breaks them all unless updated together. Known writers (Task 1 updates every one, with a grep gate):
- `sb/results.py` — `block()` synthesizes a result.
- `tests/test_results.py` — `write_result` helper.
- `tests/test_worker_loop_integration.py` — `write_result` helper.
- `tests/test_spawn.py` — `test_spawn_carries_partial_result` writes a partial.
- `.claude/skills/sb-work/task-protocol.md` and `verifier-protocol.md` — the prose result templates.
(The digest schema is independently `0.1.0` — do NOT touch it; it is a different contract.)

---

## File structure

```
schemas/result.schema.json                       # MODIFY: bump 0.1.0->0.2.0; +paused_for_research outcome; +research block
sb/results.py                                     # MODIFY: file_result delegates paused_for_research to spawn_research; +read_result(); block() -> 0.2.0
sb/cli.py                                         # MODIFY: add `sb result <id>` read verb
tests/test_results.py                             # MODIFY: write_result -> 0.2.0; +paused_for_research + read_result tests
tests/test_worker_loop_integration.py             # MODIFY: write_result -> 0.2.0
tests/test_spawn.py                               # MODIFY: partial result -> 0.2.0
tests/test_continuation_integration.py            # NEW: full chain via stub dispatcher (deterministic)
.claude/skills/sb-work/task-protocol.md           # MODIFY: result template -> 0.2.0; +Research-handoff section
.claude/skills/sb-work/verifier-protocol.md       # MODIFY: result template -> 0.2.0
.claude/skills/sb-work/SKILL.md                   # MODIFY: continuation dispatch (fetch research via `sb result`); note file-result handles paused_for_research
CLAUDE.md, docs/ROADMAP.md                        # MODIFY: mark A-continuation done
decisions/ADR-005.json, ADR-006.json              # NEW: the two design records (created at plan time, pending-review)
```

---

### Task 1: Result schema — `paused_for_research` outcome + `research` block + version bump

**Files:**
- Modify: `schemas/result.schema.json`
- Modify (version writers): `sb/results.py` (`block`), `tests/test_results.py`, `tests/test_worker_loop_integration.py`, `tests/test_spawn.py`, `.claude/skills/sb-work/task-protocol.md`, `.claude/skills/sb-work/verifier-protocol.md`
- Test: `tests/test_schemas.py` (append) or `tests/test_results.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_results.py`:
```python
def test_result_schema_v2_allows_paused_for_research(lay):
    from sb import validate
    good = {"schema_version": "0.2.0", "outcome": "paused_for_research",
            "summary": "Need a benchmark before choosing the cache design.",
            "research": {"goal": "Benchmark snapshot vs lock cache under 10k writes",
                         "tier": "haiku",
                         "done_statement": "A table comparing p50/p99 exists."}}
    validate.check("result", good)  # must not raise


def test_result_schema_rejects_old_version(lay):
    import pytest
    from sb import validate
    with pytest.raises(ValueError):
        validate.check("result", {"schema_version": "0.1.0", "outcome": "success",
                                  "summary": "x"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_results.py -k "schema_v2 or rejects_old" -v`
Expected: FAIL (current const is 0.1.0; `paused_for_research`/`research` unknown).

- [ ] **Step 3: Edit the schema**

In `schemas/result.schema.json`: change `"$id"` to `.../result/v0.2.0.json`; set `"schema_version": { "const": "0.2.0" }`; extend the outcome enum and add the `research` block:
```json
"outcome": { "enum": ["success", "partial", "blocked", "failed", "paused_for_research"] },
```
Add to `properties` (alongside the others):
```json
"research": {
  "type": "object",
  "additionalProperties": false,
  "required": ["goal", "tier", "done_statement"],
  "description": "Set with outcome=paused_for_research: the research handoff the worker (via file-result) turns into an sb spawn. The parent re-enqueues as a continuation depending on the spawned research task (§3.3).",
  "properties": {
    "goal": { "type": "string" },
    "tier": { "enum": ["fable", "opus", "sonnet", "haiku"] },
    "done_statement": { "type": "string" }
  }
}
```

- [ ] **Step 4: Update every result writer to 0.2.0**

- `sb/results.py` `block()`: the synthesized dict `{"schema_version": "0.1.0", "outcome": "blocked", ...}` → `"0.2.0"`.
- `tests/test_results.py` `write_result`: `{"schema_version": "0.1.0", ...}` → `"0.2.0"`.
- `tests/test_worker_loop_integration.py` `write_result`: `"0.1.0"` → `"0.2.0"`.
- `tests/test_spawn.py` `test_spawn_carries_partial_result`: the written partial `"0.1.0"` → `"0.2.0"`.
- `.claude/skills/sb-work/task-protocol.md` and `verifier-protocol.md`: every `schema_version: "0.1.0"` in the result templates → `"0.2.0"`.

- [ ] **Step 5: Grep gate — no stale result version remains**

Run: `grep -rn '"0.1.0"' sb/ tests/ .claude/skills/sb-work/ | grep -iv digest`
Expected: no result-schema hits remain (digest's 0.1.0 lives in `sb/digest.py`/`schemas/digest.schema.json` and is fine; confirm any remaining hit is digest-only).

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (the bump propagated; the two new schema tests green).

- [ ] **Step 7: Commit**

```bash
git add schemas/result.schema.json sb/results.py tests/test_results.py tests/test_worker_loop_integration.py tests/test_spawn.py .claude/skills/sb-work/task-protocol.md .claude/skills/sb-work/verifier-protocol.md
git commit -m "feat(sb): result schema 0.2.0 — paused_for_research outcome + research block"
```

---

### Task 2: `file-result` delegates `paused_for_research` to `spawn_research`

**Files:**
- Modify: `sb/results.py` (`file_result`)
- Test: `tests/test_results.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_results.py`:
```python
def test_paused_for_research_spawns_and_requeues_parent(lay):
    t = active_task(lay)  # active parent
    write_result(lay, t["id"], outcome="paused_for_research",
                 summary="need data first",
                 research={"goal": "benchmark the two designs", "tier": "haiku",
                           "done_statement": "a comparison table exists"})
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "queued"                          # parent re-enqueued as continuation
    lane, p = store.find_task(lay, t["id"])
    assert lane == "queued" and p["status"] == "queued"
    rid = f"{t['id']}.R1"
    assert rid in p["context"]["depends_on"]          # depends on the research task
    assert store.find_task(lay, rid)[0] == "queued"   # research task enqueued
    assert p["context"]["prior_attempts"][0]["summary"] == "need data first"  # partial carried
    assert not os.path.exists(results.result_path(lay, t["id"]))  # consumed


def test_paused_for_research_requires_research_block(lay):
    t = active_task(lay)
    write_result(lay, t["id"], outcome="paused_for_research", summary="oops no block")
    with pytest.raises(ValueError, match="research"):
        results.file_result(lay, DEFAULT_CONFIG, t["id"])
```
(`write_result` must accept and pass through a `research=` kwarg — it already spreads `**fields`, so `research={...}` flows in.)

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_results.py -k paused_for_research -v`
Expected: FAIL (`file_result` routes `paused_for_research` through `_requeue_or_fail` today, no spawn).

- [ ] **Step 3: Implement the branch**

In `sb/results.py`, add the import at top: `from sb import spawn`. Then in `file_result`, right after the active-lane check and before the normal embed/route (i.e. after `if lane != "active": raise ...`), insert — only for the author path, not verify tasks:
```python
    if not target_id and result["outcome"] == "paused_for_research":
        req = result.get("research")
        if not req:
            raise ValueError(
                "paused_for_research result must carry a `research` block "
                "{goal, tier, done_statement}")
        # spawn_research reads+consumes the parent's result file, re-enqueues the
        # parent as a continuation, and clears the lease — it owns the whole lane
        # transition, so return immediately (do NOT fall through to embed/move/
        # remove). ADR-005: file-result is the single door; spawn is the mechanism.
        research = spawn.spawn_research(
            lay, cfg, task_id, goal=req["goal"], tier=req["tier"],
            done_statement=req["done_statement"])
        return "paused" if research is None else "queued"  # None => chain-depth cap
```
Note: `target_id = task.get("context", {}).get("verifies")` is computed a few lines above; reference it. If it is not yet computed at that point in the function, move this branch to just after `target_id` is assigned.

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_results.py -k paused_for_research -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add sb/results.py tests/test_results.py
git commit -m "feat(sb): file-result delegates paused_for_research to spawn_research (ADR-005)"
```

---

### Task 3: `sb result <id>` read verb

**Files:**
- Modify: `sb/results.py` (add `read_result`), `sb/cli.py` (add verb)
- Test: `tests/test_results.py`

The worker fetches a completed research task's findings to build the continuation prompt (ADR-006). `read_result` returns the embedded `result` of a task in any lane, or `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_results.py`:
```python
def test_read_result_returns_embedded_result(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])  # embeds result, moves to paused
    got = results.read_result(lay, t["id"])
    assert got["outcome"] == "success"


def test_read_result_none_when_absent(lay):
    store.write_task(lay, "queued", make_task())  # no result embedded yet
    assert results.read_result(lay, "PLAN-001/PH-1/T-1") is None
    assert results.read_result(lay, "NOPE/PH-1/T-9") is None


def test_cli_result(lay, capsys):
    import json
    from sb import cli
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    rc = cli.main(["result", t["id"], "--repo", lay.repo])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "success"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_results.py -k "read_result or cli_result" -v`
Expected: FAIL (`read_result`/verb absent).

- [ ] **Step 3: Implement**

In `sb/results.py`:
```python
def read_result(lay, task_id):
    """The embedded result of a task in any lane, or None. The worker uses this
    to pull a completed research task's findings into a continuation prompt
    (ADR-006); read-only, never mutates."""
    _, task = store.find_task(lay, task_id)
    return task.get("result") if task else None
```
In `sb/cli.py`, add the subparser (next to `file-result`):
```python
    p = common(sub.add_parser("result"))
    p.add_argument("task_id")
```
and the handler (next to the `file-result` handler):
```python
    if a.cmd == "result":
        res = results.read_result(lay, a.task_id)
        if res is None:
            print(json.dumps({"task_id": a.task_id, "result": None}))
            return 3
        _out(res)
        return 0
```
(Exit 3 = no result yet, mirroring the "nothing to claim" convention so the worker can branch on the code.)

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_results.py -k "read_result or cli_result" -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add sb/results.py sb/cli.py tests/test_results.py
git commit -m "feat(sb): sb result <id> read verb for continuation prompts (ADR-006)"
```

---

### Task 4: Prompts — research handoff + continuation dispatch

**Files:**
- Modify: `.claude/skills/sb-work/task-protocol.md`, `.claude/skills/sb-work/SKILL.md`

Prose — reviewed against §3.3, exercised live later. No test.

- [ ] **Step 1: Add the research-handoff section to `task-protocol.md`**

After the "Hard-escalation domains" section, add:
```markdown
> ## Research handoff (when you need a DIFFERENT agent class first)
> If you cannot proceed because a question needs a different agent class /
> tier to research it (not just more work within your depth — that you do
> inline), do NOT guess and do NOT block. Instead write a `paused_for_research`
> result and stop:
> - `outcome`: `"paused_for_research"`
> - `summary`: your partial progress so far (it is carried into your
>   continuation as a prior attempt — do not lose context).
> - `research`: `{ "goal": "<one scoped research question>", "tier":
>   "fable|opus|sonnet|haiku", "done_statement": "<what the research must
>   produce>" }`.
> The worker turns this into a spawned research task and re-enqueues YOU as a
> continuation that depends on it; when the research is done you will be
> re-dispatched with its findings. Use this sparingly — only for a genuinely
> different agent class, and never deeper than the chain-depth cap (the engine
> pauses for a human past it).
```

- [ ] **Step 2: Note continuation handling in `task-protocol.md`**

In the existing "Prior attempt(s)" template area, ensure the worker-fill note covers research findings (add to "Notes for the worker filling this template"):
```markdown
- On a CONTINUATION (the task has completed research deps — ids like
  `<task>.R<n>` in `context.depends_on`), inline each research finding the
  worker fetched (`sb result <dep-id>` → its `summary`/`evidence`) under a
  "Research findings" heading, ahead of the prior-attempt partial. The subagent
  builds on the findings instead of re-researching.
```

- [ ] **Step 3: Add continuation dispatch to `SKILL.md`**

In SKILL.md step 6 (dispatch), add a bullet before filling the protocol template:
```markdown
   - **If this is a continuation** (`T.context.depends_on` contains any
     `<...>.R<n>` research ids), fetch each completed research dep with
     `sb result <dep-id>` (exit 0 → its result JSON; exit 3 → not ready, which
     should not happen since deps are `done` before claim) and pass their
     `summary`/`evidence` into the prompt as **Research findings**. This is how
     the research output reaches the continuation (ADR-006).
```
And in step 7 (file the result), add a note:
```markdown
   - A `paused_for_research` result needs **no special handling** — just
     `sb file-result <T.id>` as normal. The engine spawns the research task and
     re-enqueues this task as a continuation (ADR-005); `OUTCOME=queued`.
```

- [ ] **Step 4: Self-review against §3.3**

Re-read the worker-loop design §3.3. Confirm: a waiting parent never holds a worker (the subagent stops; the worker files and moves on); the continuation carries partial + research findings; chain-depth cap pauses for human. Fix drift inline.

- [ ] **Step 5: Commit**

```bash
git add .claude/skills/sb-work/task-protocol.md .claude/skills/sb-work/SKILL.md
git commit -m "feat(sb): research-handoff + continuation dispatch prompts (§3.3)"
```

---

### Task 5: Integration test — full continuation chain (stub dispatcher)

**Files:**
- Test: `tests/test_continuation_integration.py`

Deterministic, no model: drive the chain through real engine verbs with canned result files. This is the continuation chain D needs to exercise, captured as a regression test.

- [ ] **Step 1: Write the test**

`tests/test_continuation_integration.py`:
```python
"""Stub-dispatcher integration test for the research-handoff continuation chain
(§3.3, ADR-005/006). No model: result files are canned. Asserts the parent pauses
for research, the research task runs+verifies to done, the parent re-claims as a
continuation, and its research findings are fetchable."""
import os

from sb import claims, paths, results, store
from tests.helpers import make_task


def write_result(lay, task_id, **fields):
    r = {"schema_version": "0.2.0", "outcome": "success", "summary": "ok", **fields}
    store.write_json(os.path.join(lay.results, store.fname(task_id)), r)


def test_full_continuation_chain(lay):
    cfg = paths.load_config(lay)
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1", tier="opus"))

    # 1. claim parent, it hits a research handoff
    parent = claims.claim_one(lay, "w1", cfg=cfg)
    write_result(lay, parent["id"], outcome="paused_for_research",
                 summary="partial: scaffolded, need a benchmark",
                 research={"goal": "benchmark designs", "tier": "haiku",
                           "done_statement": "comparison table exists"})
    assert results.file_result(lay, cfg, parent["id"]) == "queued"
    rid = "PLAN-001/PH-1/T-1.R1"
    assert store.find_task(lay, rid)[0] == "queued"

    # 2. parent is NOT claimable yet (depends on the research task)
    got = claims.claim_one(lay, "w1", cfg=cfg)
    assert got["id"] == rid                       # only the research task is claimable

    # 3. research succeeds -> verify -> verdict pass -> research done
    write_result(lay, rid, outcome="success", summary="benchmark: snapshot wins p99")
    assert results.file_result(lay, cfg, rid) == "paused"
    v = claims.claim_one(lay, "w1", cfg=cfg)
    assert v["id"] == f"{rid}.V1"
    write_result(lay, v["id"], verdict="pass", verdict_notes="benchmark is sound")
    assert results.file_result(lay, cfg, v["id"]) == "done"
    assert store.find_task(lay, rid)[1]["status"] == "done"

    # 4. parent now re-claimable as a continuation; research finding is fetchable
    cont = claims.claim_one(lay, "w1", cfg=cfg)
    assert cont["id"] == parent["id"]
    assert cont["context"]["prior_attempts"][0]["summary"].startswith("partial:")
    finding = results.read_result(lay, rid)
    assert finding["summary"] == "benchmark: snapshot wins p99"
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/pytest tests/test_continuation_integration.py -v`
Expected: PASS. If a step fails, stop and investigate — it is a real finding about the chain contract.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add tests/test_continuation_integration.py
git commit -m "test(sb): full continuation-chain integration (paused_for_research -> research -> continuation)"
```

---

### Task 6: Docs — mark A-continuation done

**Files:**
- Modify: `docs/ROADMAP.md`, `CLAUDE.md`

- [ ] **Step 1: ROADMAP** — change the A-continuation row/bullet to IMPLEMENTED (date, test count from `.venv/bin/pytest -q`); note the engine (`spawn_research`) was already built and this closed the result-schema/`file-result`/`sb result`/prompt gaps; reference ADR-005/006.

- [ ] **Step 2: CLAUDE.md** — engine surface gains `result`; note `paused_for_research` outcome + the continuation chain; bump the test count; add ADR-005/006 to the AgDR list (pending-review until the gate).

- [ ] **Step 3: Final suite + commit**

```bash
.venv/bin/pytest -q
git add CLAUDE.md docs/ROADMAP.md
git commit -m "docs(sb): mark A-continuation implemented (research-handoff chain)"
```

---

## Out of scope (deferred)

- **A-planner** (`sb seed --goal` + planner protocol) — separate follow-on; the spine smoke pinned the plan-schema strictness its prompt must satisfy.
- **HDR-010 escalation layer (C)** and the **intervention-learning loop (ADR-004)** — the oversight layer.
- **D exit bar** — exercises this chain *live* with the guard hooks wired (the open seam from the 2026-06-21 spine smoke).

## Self-review checklist (run after writing, before execution)

1. **Coverage of §3.3:** signal (`paused_for_research` outcome + `research` block) → Task 1; spawn+re-enqueue (delegated to existing `spawn_research`) → Task 2; research output → continuation (worker-fetch via `sb result`) → Tasks 3+4; chain-depth cap → reused from `spawn_research` (asserted indirectly); one chain exercised → Task 5. Covered.
2. **Placeholders:** none — every code/test step shows full content; prompt sections are written out.
3. **Type/name consistency:** `read_result(lay, task_id)` used identically in module, CLI, tests, and SKILL.md; `research` block shape `{goal, tier, done_statement}` identical in schema, `file_result` branch, prompt, and tests; result `schema_version` is `"0.2.0"` everywhere after Task 1; `spawn_research` call signature matches `sb/spawn.py`.
