# A-planner Implementation Plan — `sb seed --goal` + planner protocol

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an operator hand Switchboard a raw goal (`sb seed --goal "<goal>"`) and have the worker loop dispatch a planner subagent that emits a plan-schema-valid `plans/PLAN-NNN.json` + an SDR, verified and landed for human review — reusing the existing claim/dispatch/verify spine with no schema change.

**Architecture:** `sb seed --goal` allocates the next `PLAN-NNN` and enqueues ONE ordinary task whose `done.verify.kind == "plan"` (ADR-007). The worker loop routes that discriminator to a new `planner-protocol.md`, dispatching the planner identically to any subagent. The planner commits the plan + SDR to the planning-phase branch; verification reuses the **standard verifier path** (the verifier subagent validates the committed plan against `schemas/plan.schema.json` in the branch worktree). The produced plan is a durable artifact a human reviews and expands with the existing `sb seed <plan>` — `seed --goal` does **not** auto-expand.

**Tech Stack:** Python 3 stdlib + jsonschema; pytest with the `lay` fixture (`tests/conftest.py`) and `tests/helpers.make_task`. Prompt protocols are markdown under `.claude/skills/sb-work/`.

**Grounding:** ADR-007 (this representation decision, pending-review); `docs/specs/2026-06-16-sb-worker-loop-design.md` §7; `docs/ROADMAP.md` A-planner.

**Design notes locked at plan time (not separate AgDRs — obvious lean calls):**
- **Planner-task id** = `PLAN-NNN/PH-0/T-1`. `PH-0` is a synthetic planning-phase container (matches the task schema `source.phase_id` pattern `^PH-[0-9]{1,}$`). `T-1` matches `^(T-[0-9]{1,}|GATE)$`.
- **No gate task** for `seed --goal`. The "every phase ends at a GATE" invariant governs *seeded plans* (`seed.seed` adds them); `seed --goal` is a single bootstrap task, and the plan it produces carries its own per-phase gates. Adding a gate that gates nothing is ceremony. The human review surface is the plan artifact + the explicit act of running `sb seed <plan>`.
- **`PLAN-NNN` allocation** scans both existing `plans/PLAN-*.json` files AND in-flight planner task ids across all lanes (an allocated-but-not-yet-emitted id), takes max + 1. No re-seed guard: each `seed --goal` call is an explicit "plan this", so two calls produce two plans — acceptable.
- **Planner tier** defaults to `opus` (planning is heavy; plan schema says "Fable, or Opus for lighter goals"), overridable via `--tier`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `sb/seed.py` | plan→queue expansion; **add** `allocate_plan_id` + `seed_goal` | Modify |
| `sb/cli.py` | verb wiring; **make** `seed` accept `--goal` xor `--plan`, add `--tier` | Modify |
| `tests/test_seed_goal.py` | unit tests for `seed_goal` + allocation | Create |
| `tests/test_cli.py` | CLI test for `sb seed --goal` | Modify |
| `tests/test_results.py` | engine-integration: planner task reuses the verify path | Modify |
| `.claude/skills/sb-work/SKILL.md` | step 6 routing: `verify.kind == "plan"` → planner-protocol | Modify (reviewed) |
| `.claude/skills/sb-work/planner-protocol.md` | the planner dispatch prompt (carries the exact plan schema) | Create (reviewed) |
| `.claude/skills/sb-work/verifier-protocol.md` | machine-check branch for `kind == "plan"` | Modify (reviewed) |
| `docs/ROADMAP.md`, `CLAUDE.md` | status: A-planner implemented | Modify |

Tasks 1–3 are TDD (engine). Tasks 4–6 are **reviewed-not-tested prose** (the prompt protocols — per spec §6/§7, exercised live in D, not by a green test). Each prose task carries a review checklist instead of a pytest step.

---

### Task 1: `seed.seed_goal` + `allocate_plan_id` (engine)

**Files:**
- Modify: `sb/seed.py`
- Test: `tests/test_seed_goal.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_seed_goal.py
import os

from sb import seed, store, validate


def test_seed_goal_creates_one_planner_task(lay):
    cid = seed.seed_goal(lay, "build a thing")
    assert cid == "PLAN-001/PH-0/T-1"
    lane, t = store.find_task(lay, cid)
    assert lane == "queued"
    assert t["goal"] == "build a thing"
    assert t["tier"] == "opus"
    assert t["context"]["branch"] == "sb/plan-001/ph-0"
    assert t["context"]["depends_on"] == []
    # The discriminator the worker loop routes on (ADR-007):
    assert t["done"]["verify"]["kind"] == "plan"
    assert t["done"]["verify"]["ref"] == "plans/PLAN-001.json"
    assert "plans/PLAN-001.json" in t["done"]["statement"]
    # No gate task is created for seed --goal:
    assert store.list_tasks(lay, "paused") == []


def test_seed_goal_task_is_schema_valid(lay):
    cid = seed.seed_goal(lay, "g")
    _, t = store.find_task(lay, cid)
    validate.check("task", t)  # raises if invalid


def test_seed_goal_first_id_is_plan_001(lay):
    assert seed.seed_goal(lay, "g").startswith("PLAN-001/")


def test_seed_goal_allocates_next_id_past_existing_plan_file(lay):
    store.write_json(os.path.join(lay.plans, "PLAN-001.json"), {})
    assert seed.seed_goal(lay, "g").startswith("PLAN-002/")


def test_seed_goal_allocates_next_id_past_in_flight_planner_task(lay):
    seed.seed_goal(lay, "g1")            # PLAN-001 planner task queued
    assert seed.seed_goal(lay, "g2").startswith("PLAN-002/")


def test_seed_goal_tier_override(lay):
    cid = seed.seed_goal(lay, "g", tier="fable")
    _, t = store.find_task(lay, cid)
    assert t["tier"] == "fable"


def test_seed_goal_repo_state_carried(lay):
    cid = seed.seed_goal(lay, "g", repo_state="deadbeef")
    _, t = store.find_task(lay, cid)
    assert t["context"]["repo_state"] == "deadbeef"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_seed_goal.py -v`
Expected: FAIL with `AttributeError: module 'sb.seed' has no attribute 'seed_goal'`

- [ ] **Step 3: Implement `allocate_plan_id` + `seed_goal`**

Add `import os` and `import re` at the top of `sb/seed.py` (it currently imports only `datetime as dt`, `store`, `validate`, `LANES`). Then append:

```python
def allocate_plan_id(lay):
    """Next free PLAN-NNN. Scans emitted plan files AND in-flight planner task
    ids (an id allocated by an earlier seed_goal whose plan isn't written yet),
    so two seed_goal calls never collide."""
    nums = []
    if os.path.isdir(lay.plans):
        for f in os.listdir(lay.plans):
            m = re.match(r"PLAN-(\d+)\.json$", f)
            if m:
                nums.append(int(m.group(1)))
    for lane in LANES:
        for t in store.list_tasks(lay, lane):
            m = re.match(r"PLAN-(\d+)/", t["id"])
            if m:
                nums.append(int(m.group(1)))
    return f"PLAN-{(max(nums) + 1) if nums else 1:03d}"


def seed_goal(lay, goal, repo_state="HEAD", tier="opus"):
    """Enqueue ONE planner task for a raw goal. The worker loop routes it to the
    planner protocol via done.verify.kind == 'plan' (ADR-007); the planner emits
    plans/<plan_id>.json + an SDR, verified by the standard verifier path. No
    gate task: the gate invariant governs the seeded plan this PRODUCES."""
    plan_id = allocate_plan_id(lay)
    branch = f"sb/{plan_id}/PH-0".lower()
    cid = composite(plan_id, "PH-0", "T-1")
    plan_path = f"plans/{plan_id}.json"
    task = {
        "schema_version": "0.2.0",
        "id": cid,
        "tier": tier,
        "status": "queued",
        "source": {"plan_id": plan_id, "phase_id": "PH-0", "task_id": "T-1"},
        "goal": goal,
        "context": {
            "repo_state": repo_state,
            "branch": branch,
            "chain_depth": 0,
            "grounding": [],
            "constraints": [],
            "depends_on": [],
        },
        "done": {
            "statement": (
                f"A plan-schema-valid plan for the goal is committed at "
                f"{plan_path}, with an SDR recording the decomposition "
                f"rationale and set as the plan's decision_ref."),
            "verify": {"kind": "plan", "ref": plan_path},
        },
        "attempts": 0,
        "created_at": now_iso(),
        "created_by": "sb",
    }
    store.write_task(lay, "queued", task)
    return cid
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_seed_goal.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add sb/seed.py tests/test_seed_goal.py
git commit -m "feat(sb): seed --goal engine — seed_goal + allocate_plan_id (A-planner, ADR-007)"
```

---

### Task 2: CLI — `sb seed --goal` (xor `--plan`), `--tier`

**Files:**
- Modify: `sb/cli.py:45-49` (the `seed` subparser) and `sb/cli.py:133-142` (the `seed` handler)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cli.py` (it already imports `from sb import cli` and uses `capsys` + the `lay`/`tmp_path` pattern — match the existing style in that file; the snippet below uses `--repo`):

```python
def test_cli_seed_goal_enqueues_planner_task(lay, capsys):
    from sb import cli, store
    rc = cli.main(["seed", "--repo", lay.repo, "--goal", "build a thing"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seeded"] == ["PLAN-001/PH-0/T-1"]
    _, t = store.find_task(lay, "PLAN-001/PH-0/T-1")
    assert t["done"]["verify"]["kind"] == "plan"


def test_cli_seed_requires_plan_xor_goal(lay):
    from sb import cli
    with pytest.raises(SystemExit):  # argparse: neither given
        cli.main(["seed", "--repo", lay.repo])
```

(If `json`/`pytest` are not already imported at the top of `tests/test_cli.py`, add `import json` and `import pytest`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_cli.py -k seed_goal -v`
Expected: FAIL — `seed` currently requires `--plan`, so `--goal` is an unrecognized argument (argparse SystemExit) or the second test passes for the wrong reason.

- [ ] **Step 3: Change the subparser to a mutually-exclusive group**

Replace `sb/cli.py:45-49`:

```python
    p = common(sub.add_parser("seed"))
    p.add_argument("--plan", required=True)
    p.add_argument("--repo-state", default="HEAD")
    p.add_argument("--force", action="store_true")
```

with:

```python
    p = common(sub.add_parser("seed"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", help="path to a plan json to expand into tasks")
    g.add_argument("--goal", help="raw goal; enqueues one planner task")
    p.add_argument("--repo-state", default="HEAD")
    p.add_argument("--tier", default="opus",
                   help="planner tier for --goal (default opus)")
    p.add_argument("--force", action="store_true")
```

- [ ] **Step 4: Branch the handler on `--goal`**

Replace `sb/cli.py:133-142` (the `if a.cmd == "seed":` block):

```python
    if a.cmd == "seed":
        if a.goal:
            cid = seed.seed_goal(lay, a.goal, repo_state=a.repo_state,
                                 tier=a.tier)
            _out({"seeded": [cid]})
            return 0
        plan = store.read_json(a.plan)
        try:
            seeded = seed.seed(lay, plan, repo_state=a.repo_state,
                               force=a.force)
        except (seed.BlockingQuestions, seed.AlreadySeeded) as e:
            print(json.dumps({"held": str(e)}), file=sys.stderr)
            return 2
        _out({"seeded": seeded})
        return 0
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_cli.py -k seed -v`
Expected: PASS (both new tests + any existing `seed` CLI tests still green)

- [ ] **Step 6: Commit**

```bash
git add sb/cli.py tests/test_cli.py
git commit -m "feat(sb): sb seed --goal CLI (plan xor goal, --tier) (A-planner)"
```

---

### Task 3: Engine-integration — a planner task reuses the verify path

Proves ADR-007's core claim: the planner task needs **no special engine path** — `file-result` on a `success` outcome enqueues a verifier exactly as for any task, and a `pass` verdict moves the planner task to `done`. Uses stub result files (no real plan, no subagent), mirroring `tests/test_results.py` style.

**Files:**
- Modify: `tests/test_results.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_results.py` (it already imports `from sb import results, store` and uses the `lay` fixture + writes result files to `results.result_path(lay, id)`; match that style):

```python
def test_planner_task_success_enqueues_plan_verifier_then_done(lay):
    from sb import paths, results, seed, store
    cfg = paths.load_config(lay)
    cid = seed.seed_goal(lay, "build a thing")        # PLAN-001/PH-0/T-1, queued
    store.move_task(lay, "queued", "active", cid)     # simulate a claim

    # planner subagent filed success
    store.write_json(results.result_path(lay, cid), {
        "schema_version": "0.2.0", "outcome": "success",
        "summary": "Emitted plans/PLAN-001.json + SDR-010.",
        "evidence": [{"kind": "commit", "ref": "abc123"}],
    })
    assert results.file_result(lay, cfg, cid) == "paused"  # awaiting_verification

    # a verifier task was enqueued carrying the plan machine-check
    vlane, vtask = store.find_task(lay, cid + ".V1")
    assert vlane == "queued"
    assert vtask["context"]["verifies"] == cid
    assert vtask["done"]["verify"]["kind"] == "plan"
    assert vtask["done"]["verify"]["ref"] == "plans/PLAN-001.json"

    # verifier passes → planner task reaches done (standard verdict path)
    store.move_task(lay, "queued", "active", cid + ".V1")
    store.write_json(results.result_path(lay, cid + ".V1"), {
        "schema_version": "0.2.0", "outcome": "success", "verdict": "pass",
        "summary": "Validated plans/PLAN-001.json against the plan schema; phases gated.",
    })
    assert results.file_result(lay, cfg, cid + ".V1") == "done"
    lane, t = store.find_task(lay, cid)
    assert lane == "done" and t["status"] == "done"
```

- [ ] **Step 2: Run the test to verify it fails, then passes with no engine change**

Run: `python3 -m pytest tests/test_results.py -k planner_task_success -v`
Expected: **PASS immediately** — this is a characterization test confirming the existing engine already handles planner tasks via the standard path. If it FAILS, the failure is a real finding (the reuse claim is wrong); stop and investigate before proceeding. Do not add engine branching to make it pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_results.py
git commit -m "test(sb): pin planner-task verification reuses the standard verify path (ADR-007)"
```

---

### Task 4: SKILL.md routing branch (reviewed-not-tested)

**Files:**
- Modify: `.claude/skills/sb-work/SKILL.md` (step 6, the protocol-selection bullets ~lines 76–77)

- [ ] **Step 1: Add the planner branch**

Replace these two lines:

```
   - If `T.context.verifies` is set → use **verifier-protocol.md**.
   - Otherwise → use **task-protocol.md**.
```

with:

```
   - If `T.context.verifies` is set → use **verifier-protocol.md**.
   - Else if `T.done.verify.kind == "plan"` → use **planner-protocol.md** (the
     task is a `sb seed --goal` planner unit; ADR-007). Fill `{plan_id}` from
     `T.source.plan_id` and `{plan_path}` from `T.done.verify.ref`.
   - Otherwise → use **task-protocol.md**.
```

- [ ] **Step 2: Review checklist** (no pytest — this is prose, exercised live in D)

  - [ ] The branch order puts `verifies` first (a verifier OF a planner task must still use verifier-protocol, not planner-protocol — its `verify.kind` is also `plan`, so order matters).
  - [ ] `{plan_id}` and `{plan_path}` are the only new template variables, and both are sourced from fields that exist on a `seed_goal` task (`source.plan_id`, `done.verify.ref`).
  - [ ] No other step in SKILL.md assumes exactly two protocols (grep `task-protocol\|verifier-protocol` in SKILL.md; confirm none break).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/SKILL.md
git commit -m "feat(sb-work): route verify.kind==plan to planner-protocol (A-planner)"
```

---

### Task 5: `planner-protocol.md` (reviewed-not-tested)

**Files:**
- Create: `.claude/skills/sb-work/planner-protocol.md`

- [ ] **Step 1: Write the protocol** (carries the EXACT plan schema — ROADMAP requirement)

```markdown
# Planner subagent protocol

The worker fills this template for a **planner task** (`T.done.verify.kind ==
"plan"`, produced by `sb seed --goal`) and dispatches it to a fresh-context
subagent at `T.tier`. The deliverable is NOT code: it is a plan-schema-valid
`plans/{plan_id}.json` plus a synthesis decision (SDR) recording why the
decomposition/routing was chosen. The worker does not seed the plan — a human
reviews it and runs `sb seed --plan plans/{plan_id}.json`.

## Dispatch prompt template

> You are a **planner** in a fresh context. Decompose one goal into an
> executable, schema-valid plan. You write a plan + a decision record; you do
> NOT write the implementation and you do NOT seed the plan.
>
> **Working directory (CWD):** `{worktree_path}` — a git worktree of branch
> `{branch}`. Write and commit your plan + SDR here.
>
> **Result file path:** `{result_path}` — an ABSOLUTE path in the main repo (NOT
> under your CWD). Write your result file to exactly this path.
>
> **Goal (verbatim — preserve it as `goal` in the plan):** {T.goal}
>
> **Plan id you MUST use:** `{plan_id}`  →  write the plan to `{plan_path}`.
>
> **Grounding (read before planning — precedent, not first principles):**
> {for each id in T.context.grounding: the digest from `sb query`; or "query the
> decision log with `sb query --tags <relevant>` and ground the plan in what you
> find"}
>
> ## What to produce
> 1. A plan at `{plan_path}` that VALIDATES against `schemas/plan.schema.json`
>    (v0.1.0). The schema is strict (`additionalProperties:false` throughout).
>    Required top-level: `schema_version:"0.1.0"`, `plan_id:"{plan_id}"`, `goal`
>    (the verbatim goal above), `created` (ISO-8601), `author:{kind:"model",
>    id:"<your model id>"}`, and `phases` (≥1). Each phase requires `phase_id`
>    (`^PH-[0-9]+$`), `name`, `default_model` (one of fable|opus|sonnet|haiku —
>    the cost lever), and `tasks` (≥1); give every phase a `gate:{type:"human"|
>    "auto", condition:"<expr>"}` (the phase-transition forcing function — every
>    phase ends at a human review gate). Each task requires `task_id`
>    (`^T-[0-9]+$`), `title`, and `done:{statement, verify?:{kind,ref,expect?}}`.
>    Optional but encouraged: task `model` overrides, `depends_on` (intra-plan
>    task DAG; NO forward deps into a later phase — they deadlock behind the
>    gate), `constraints`, `grounding` (ADR/HDR/SDR ids), `open_questions`
>    (blocking → a human must answer before execution; resolve_by:"research" →
>    an agent investigates inside a named phase), and `intent` per phase.
> 2. A synthesis decision at `decisions/SDR-NNN.json` (next free SDR id; scan
>    `decisions/` for the max) validating against
>    `schemas/decision-record.schema.json` (v0.3.0): `type:"synthesis"`,
>    `status:"pending-review"`, `id:"SDR-NNN"`, your `author`, a `title`,
>    `context`, the `options` you weighed for the decomposition/routing, the
>    `chosen` shape, `reasoning`, and `blast_radius`. Set the plan's
>    `decision_ref` to this SDR id.
>
> ## Sizing & routing guidance
> - One task = one scoped, verifiable outcome. If a task needs an "and", split it.
> - Route cheap/mechanical tasks to `haiku`/`sonnet`; reserve `opus`/`fable` for
>   design and cross-cutting decisions. `default_model` per phase; override per task.
> - Prefer phases that each end in a reviewable PR (the gate).
>
> ## Before you file — self-validate
> Run this in your CWD and make sure it exits 0:
> ```
> python3 -c "from sb import validate, store; validate.check('plan', store.read_json('{plan_path}')); print('plan ok')"
> python3 -c "from sb import validate, store; validate.check('decision', store.read_json('decisions/SDR-NNN.json')); print('sdr ok')"
> ```
>
> ## Finishing
> 1. Commit `{plan_path}` and `decisions/SDR-NNN.json` to branch `{branch}`.
> 2. Write your result file to `{result_path}` (the exact absolute path above):
>    - `schema_version: "0.2.0"`
>    - `outcome: "success"` (plan + SDR committed and self-validated) | `blocked`
>      (the goal is under-specified to the point you cannot plan it — say what is
>      missing) | `partial`.
>    - `summary`: 2–3 sentences — the shape of the plan (phases, key routing).
>    - `evidence`: `[{kind:"commit", ref:"<sha>"}]`.
>    - `decisions_emitted`: `["SDR-NNN"]`.
> 3. Do not seed, do not move lanes, do not run any other `sb` command — the
>    worker files your result; verification validates the plan independently.

## Notes for the worker filling this template

- `{plan_id}` = `T.source.plan_id`; `{plan_path}` = `T.done.verify.ref`.
- Resolve `grounding` ids through `sb query` so the planner gets digests.
- The CWD line is mandatory (isolation guarantee, PHI-033).
- `{result_path}` must be the absolute main-repo path the engine reads:
  `<repo>/.switchboard/results/<id-with-/-as-_>.json`.
- The planner PROMPT is the lowest-confidence artifact of A-planner (ADR-007):
  it is reviewed-not-tested and gets its live exercise in the D exit bar.
```

- [ ] **Step 2: Review checklist**

  - [ ] The plan-schema requirements inlined above match `schemas/plan.schema.json` field-for-field (required keys, the `gate.{type,condition}` per phase, `author.{kind,id}`, the `^PH-/^T-` patterns). Re-read the schema and diff.
  - [ ] The self-validate commands use the real module API (`validate.check('plan'|'decision', store.read_json(path))`) — confirmed against `sb/validate.py`.
  - [ ] The protocol forbids seeding and forbids forward deps (deadlock behind the gate — same rule `seed.seed` enforces at `tests/test_seed.py::test_forward_dep_rejected`).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/planner-protocol.md
git commit -m "feat(sb-work): planner-protocol.md — goal→plan+SDR dispatch (A-planner)"
```

---

### Task 6: verifier-protocol — `kind == "plan"` machine check (reviewed-not-tested)

**Files:**
- Modify: `.claude/skills/sb-work/verifier-protocol.md` (the "What to do" step 1)

- [ ] **Step 1: Add the plan-validation branch**

In the "## What to do" list, replace step 1:

```
> 1. **Run the machine check** (`{target.done.verify.ref}`) yourself in the
>    worktree and record its real result. If there is no machine check, inspect
>    the committed diff directly.
```

with:

```
> 1. **Run the machine check** and record its real result:
>    - If `{target.done.verify.kind}` is `plan`, the check is plan-schema
>      validation of the file at `{target.done.verify.ref}`. Run, in the worktree:
>      `python3 -c "from sb import validate, store; validate.check('plan', store.read_json('{target.done.verify.ref}')); print('plan ok')"`
>      (exit 0 = valid). Then judge the plan on substance: ≥1 phase, EVERY phase
>      ends at a `gate`, no forward task deps into a later phase, `goal` matches
>      the original ask, routing (`default_model`/`model`) is sane, and a
>      `decision_ref` SDR exists. A schema-valid but vacuous plan is a `fail`.
>    - Otherwise run `{target.done.verify.ref}` yourself and record its result;
>      if there is no machine check, inspect the committed diff directly.
```

- [ ] **Step 2: Review checklist**

  - [ ] The substance checks mirror the planner-protocol's "What to produce" (gates per phase, no forward deps, decision_ref) — a verifier should fail exactly what the planner was told to produce.
  - [ ] The verifier still files `outcome:"success"` + `verdict:"pass"|"fail"` (the verdict path is unchanged — only the machine-check definition grew).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/verifier-protocol.md
git commit -m "feat(sb-work): verifier handles verify.kind==plan (schema-validate the emitted plan)"
```

---

### Task 7: Full suite green + docs

**Files:**
- Modify: `docs/ROADMAP.md`, `CLAUDE.md`

- [ ] **Step 1: Run the whole suite**

Run: `python3 -m pytest -q`
Expected: PASS — all prior tests (182 at A-continuation landing) plus the new `test_seed_goal.py`, the `test_cli.py` seed-goal tests, and the `test_results.py` planner-path test. Record the new total.

- [ ] **Step 2: Update ROADMAP** — flip the A-planner row in the milestone table to `IMPLEMENTED <date> — <N> tests`, mark the "Open design question" resolved by **ADR-007**, and note the planner-protocol is reviewed-not-tested (live in D).

- [ ] **Step 3: Update `CLAUDE.md` State** — A-planner IMPLEMENTED; add `seed --goal` to the engine surface line; set the new **Next:** to **C** (HDR-010 escalation + ADR-004 learning loop); add ADR-007 to the pending-review AgDR list for the gate.

- [ ] **Step 4: Commit**

```bash
git add docs/ROADMAP.md CLAUDE.md
git commit -m "docs(sb): A-planner implemented; ADR-007 resolves the representation question"
```

---

## Self-Review

**Spec coverage** (ROADMAP A-planner + spec §7):
- `sb seed --goal` entry → Tasks 1–2. ✓
- Planner dispatched identically (only entry point + plan/SDR emission new) → Task 4 (one routing branch), Task 5 (protocol). ✓
- Planner emits plan-schema-valid plans; prompt carries the exact schema → Task 5 inlines the schema + self-validate step. ✓
- SDR emission (`plan.decision_ref`) → Task 5. ✓
- Verification of the plan → Task 6 (verifier `kind==plan` branch) + Task 3 (engine reuse pinned). ✓
- Representation open question resolved → ADR-007 (recorded; this plan builds on it). ✓

**Placeholder scan:** no TBD/"handle edge cases"/"similar to" — every code/prose step shows full content. Prose tasks (4–6) carry review checklists in lieu of pytest, explicitly because the spec defines prompt protocols as reviewed-not-tested. ✓

**Type/name consistency:** `seed_goal(lay, goal, repo_state, tier)` and `allocate_plan_id(lay)` are used identically in `sb/seed.py`, `sb/cli.py`, and all three test files; the planner-task shape (`done.verify.kind=="plan"`, `done.verify.ref=="plans/PLAN-NNN.json"`, id `PLAN-NNN/PH-0/T-1`, branch `sb/plan-nnn/ph-0`) is asserted identically across Tasks 1, 2, 3 and consumed by Tasks 4–6. ✓
