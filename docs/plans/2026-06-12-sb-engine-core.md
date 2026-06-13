# sb Engine Core Implementation Plan (M0, Plan 1 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the deterministic `sb` engine — schemas, filesystem queue with leases, DAG-guarded spawn/continuations, verification-lane routing, seeding, and decision queries — fully unit-tested, per the v2 design (`docs/specs/2026-06-12-switchboard-v2-design.md` §2–§6).

**Architecture:** A single Python package `sb/` of small focused modules (paths, validate, store, leases, claims, dag, spawn, results, seed, query) behind one argparse CLI. All state lives under `.switchboard/` in a target repo; every task write passes jsonschema validation; lane transitions are atomic `os.rename`. No model calls anywhere in this plan — judgment arrives in Plan 3.

**Tech Stack:** Python 3.11+, `jsonschema`, `pytest`. No other dependencies.

**Scope notes:** Plan 2 covers `sb brief/status/stamp` + notify hook. Plan 3 covers the `/sb-work` skill, subagent prompt protocols, tripwire hooks, and the M0 end-to-end exit bar (including the idle-poll research task). Out of scope here by design.

---

## File structure

```
pyproject.toml                  # package + sb entrypoint + pytest config
sb/__init__.py
sb/paths.py                     # .switchboard layout, init, config defaults
sb/validate.py                  # jsonschema choke point
sb/store.py                     # task io, atomic lane moves, lookup
sb/leases.py                    # lease files, expiry
sb/claims.py                    # deps_met, claim, claim --wait, requeue-stale, heartbeat
sb/dag.py                       # cycle detection, addition checks
sb/spawn.py                     # research handoff + parent continuation
sb/results.py                   # file-result routing + verification lane
sb/seed.py                      # plan -> queue expansion (v2: branch, gate-per-phase)
sb/query.py                     # decision retrieval from decisions/
sb/cli.py                       # argparse wiring
schemas/result.schema.json      # NEW: the result contract
schemas/task.schema.json        # MODIFIED: v0.2.0
schemas/decision-record.schema.json  # MODIFIED: v0.3.0 (steelman, blast_radius)
tests/__init__.py
tests/helpers.py                # make_task fixture-builder
tests/conftest.py               # lay fixture
tests/test_{paths,validate,store,leases,claims,dag,spawn,results,seed,query,cli}.py
```

Convention notes for the implementer:
- Task ids are composites `PLAN-001/PH-1/T-1`; on disk `/` becomes `_`.
- Research tasks append `.R<n>` to the parent id; verification tasks `.V<n>`.
- `attempts` increments only on requeue-after-failure (partial/failed outcome or verifier rejection), never on claim or stale-requeue — this is the infra-failure ≠ task-failure rule (spec §8).
- The embedded `result` object inside a task file is validated against `schemas/result.schema.json` at the `file-result` boundary (the only door results enter through); `task.schema.json` keeps `result` as a loose object with a comment pointing at the result schema. One contract, no drift (shared-spec principle).

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `sb/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_scaffold.py`

- [ ] **Step 1: Write pyproject.toml**

```toml
[project]
name = "switchboard"
version = "0.2.0"
description = "Switchboard sb engine: deterministic file-queue orchestration"
requires-python = ">=3.11"
dependencies = ["jsonschema>=4.21"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
sb = "sb.cli:main"

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["sb"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create package and test stubs**

`sb/__init__.py`:
```python
__version__ = "0.2.0"
```

`tests/__init__.py`: empty file.

`tests/test_scaffold.py`:
```python
import sb


def test_package_imports():
    assert sb.__version__ == "0.2.0"
```

- [ ] **Step 3: Install and run**

Run: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest tests/test_scaffold.py -v`
Expected: 1 passed. (Add `.venv/` to `.gitignore`.)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml sb/__init__.py tests/__init__.py tests/test_scaffold.py .gitignore
git commit -m "feat(sb): project scaffold with pytest + jsonschema"
```

---

### Task 2: Schema updates and the result contract

**Files:**
- Create: `schemas/result.schema.json`
- Modify: `schemas/task.schema.json`
- Modify: `schemas/decision-record.schema.json`
- Test: `tests/test_schemas.py`

- [ ] **Step 1: Write the failing test**

`tests/test_schemas.py`:
```python
import json

import pytest
from jsonschema import Draft202012Validator

SCHEMAS = "schemas"


def load(name):
    with open(f"{SCHEMAS}/{name}", encoding="utf-8") as f:
        return Draft202012Validator(json.load(f))


GOOD_RESULT = {
    "schema_version": "0.1.0",
    "outcome": "success",
    "summary": "Implemented the parser; tests green.",
    "evidence": [{"kind": "test", "ref": "tests/test_parser.py", "result": "pass"}],
    "decisions_emitted": ["ADR-051"],
    "completed_at": "2026-06-12T00:00:00+00:00",
}


def test_result_schema_accepts_valid():
    load("result.schema.json").validate(GOOD_RESULT)


def test_result_schema_accepts_verdict():
    r = dict(GOOD_RESULT, verdict="fail", verdict_notes="stress test flaked twice")
    load("result.schema.json").validate(r)


def test_result_schema_rejects_unknown_field():
    v = load("result.schema.json")
    assert not v.is_valid(dict(GOOD_RESULT, surprise=1))


def test_result_schema_rejects_bad_outcome():
    v = load("result.schema.json")
    assert not v.is_valid(dict(GOOD_RESULT, outcome="meh"))


def test_task_schema_accepts_v2_context_fields():
    task = {
        "schema_version": "0.2.0",
        "id": "PLAN-001/PH-1/T-1",
        "tier": "haiku",
        "status": "awaiting_verification",
        "source": {"plan_id": "PLAN-001", "phase_id": "PH-1", "task_id": "T-1"},
        "goal": "do the thing",
        "context": {
            "repo_state": "HEAD",
            "branch": "sb/plan-001/ph-1",
            "chain_depth": 1,
            "verifies": "PLAN-001/PH-1/T-0",
            "prior_attempts": [{"anything": "goes here"}],
            "depends_on": [],
        },
        "done": {"statement": "thing is done"},
        "attempts": 0,
        "created_at": "2026-06-12T00:00:00+00:00",
        "created_by": "test",
    }
    load("task.schema.json").validate(task)


def test_task_schema_accepts_gate_source():
    task_id = {"plan_id": "PLAN-001", "phase_id": "PH-1", "task_id": "GATE"}
    schema = load("task.schema.json")
    # validate just the source subobject through a full task
    task = {
        "schema_version": "0.2.0",
        "id": "PLAN-001/PH-1/GATE",
        "tier": "fable",
        "status": "paused_for_human",
        "source": task_id,
        "goal": "Human review gate",
        "context": {"repo_state": "HEAD", "chain_depth": 0, "depends_on": []},
        "done": {"statement": "phase PR merged"},
        "attempts": 0,
        "created_at": "2026-06-12T00:00:00+00:00",
        "created_by": "sb",
    }
    schema.validate(task)


def test_decision_schema_accepts_steelman_and_blast_radius():
    rec = {
        "schema_version": "0.3.0",
        "id": "ADR-051",
        "type": "agent",
        "status": "proposed",
        "timestamp": "2026-06-12T00:00:00+00:00",
        "title": "Use immutable snapshots",
        "author": {"kind": "model", "id": "claude-opus-4-8"},
        "steelman": [
            {"option": "mutable-with-locks", "strongest_case": "Lower memory; familiar pattern."}
        ],
        "blast_radius": "Cache module only; no API surface change.",
    }
    load("decision-record.schema.json").validate(rec)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_schemas.py -v`
Expected: FAIL — `result.schema.json` missing; task/decision schemas reject new fields.

- [ ] **Step 3: Create `schemas/result.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://colin-prologue/schemas/result/v0.1.0.json",
  "title": "Task Result",
  "description": "The structured file a task subagent writes to .switchboard/results/<id>.json. The ONLY door a result enters the system through; sb file-result validates here, then embeds it in the task file. Verification results additionally carry a verdict.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "outcome", "summary"],
  "properties": {
    "schema_version": { "const": "0.1.0" },
    "outcome": { "enum": ["success", "partial", "blocked", "failed"] },
    "summary": { "type": "string", "description": "2-3 sentences. A handoff digest, never a transcript." },
    "evidence": {
      "type": "array",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["kind", "ref"],
        "properties": {
          "kind": { "type": "string", "examples": ["commit", "test", "metric", "diff", "pr"] },
          "ref": { "type": "string" },
          "result": { "type": "string" }
        }
      }
    },
    "decisions_emitted": {
      "type": "array",
      "items": { "type": "string", "pattern": "^(ADR|HDR|SDR)-[0-9]{3,}$" }
    },
    "unblocks": { "type": "array", "items": { "type": "string" } },
    "verdict": { "enum": ["pass", "fail"], "description": "Verification tasks only." },
    "verdict_notes": { "type": "string" },
    "completed_at": { "type": "string", "format": "date-time" }
  }
}
```

- [ ] **Step 4: Modify `schemas/task.schema.json`**

Exact edits (the rest of the file is unchanged):

1. Root: `"schema_version": { "const": "0.1.0" }` → `"schema_version": { "const": "0.2.0" }`.
2. `status.enum`: add `"awaiting_verification"` after `"blocked"`.
3. `source.properties.task_id.pattern`: `"^T-[0-9]{1,}$"` → `"^(T-[0-9]{1,}|GATE)$"`.
4. Inside `context.properties`, add these four properties (after `depends_on`):

```json
"branch": { "type": "string", "description": "Phase branch the task's worktree is cut from, e.g. 'sb/plan-001/ph-2'." },
"chain_depth": { "type": "integer", "minimum": 0, "description": "Research-handoff recursion depth. sb spawn increments; max enforced from config (default 3)." },
"verifies": { "type": "string", "description": "Set on verification tasks: the task id whose result this task judges. Tasks with this set are never themselves re-verified." },
"prior_attempts": { "type": "array", "items": { "type": "object" }, "description": "Results of earlier attempts/continuations, carried into the retry prompt so retries are never blind." }
```

5. Replace the entire `result` property (the strict object) with:

```json
"result": {
  "type": "object",
  "$comment": "Validated against schemas/result.schema.json at the sb file-result boundary — the only entry door. Kept loose here so the two schemas cannot drift.",
  "description": "The embedded copy of the result file, attached when filed."
}
```

6. Update the `id` description's claim convention text in `claim` if present is unchanged; in the top-level `description`, replace `(move queued -> active + commit + push)` in the `claim` property description with `(atomic rename queued -> active + lease file)`.

- [ ] **Step 5: Modify `schemas/decision-record.schema.json`**

1. `"schema_version": { "const": "0.2.0" }` → `{ "const": "0.3.0" }` and `$id` version `v0.2.0` → `v0.3.0`.
2. Add to root `properties` (after `evidence`):

```json
"steelman": {
  "type": "array",
  "description": "PHI-028: the strongest honest case FOR each rejected option, written by the decider. Counters self-justification; required by convention for AgDR-class records (enforced in review, not schema).",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "required": ["option", "strongest_case"],
    "properties": {
      "option": { "type": "string", "description": "Must match an options[].name." },
      "strongest_case": { "type": "string" }
    }
  }
},
"blast_radius": {
  "type": "string",
  "description": "PHI-028: what this decision touches and how expensive reversal is — enables async review at the PR gate."
}
```

Note: existing records on disk declare `"schema_version": "0.2.0"` and will fail the new const. That is intended — the v1 demo records in `.decisions/` are slated for removal (spec §9); the real records in `decisions/` are HDRs maintained by hand. Do not migrate them in this task.

- [ ] **Step 6: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_schemas.py -v`
Expected: 7 passed.

- [ ] **Step 7: Commit**

```bash
git add schemas/ tests/test_schemas.py
git commit -m "feat(schemas): result contract, task v0.2.0 (verification+continuation), decision v0.3.0 (steelman, blast_radius)"
```

---

### Task 3: Layout, init, config

**Files:**
- Create: `sb/paths.py`
- Test: `tests/test_paths.py`, `tests/conftest.py`

- [ ] **Step 1: Write the failing test**

`tests/conftest.py`:
```python
import pytest

from sb import paths


@pytest.fixture
def lay(tmp_path):
    return paths.init(str(tmp_path))
```

`tests/test_paths.py`:
```python
import json
import os

from sb import paths


def test_init_creates_layout(tmp_path):
    lay = paths.init(str(tmp_path))
    for lane in paths.LANES:
        assert os.path.isdir(lay.lane(lane))
    for d in [lay.leases, lay.heartbeats, lay.results, lay.decisions, lay.plans]:
        assert os.path.isdir(d)
    assert os.path.isfile(lay.config_path)


def test_init_is_idempotent_and_preserves_config(tmp_path):
    lay = paths.init(str(tmp_path))
    with open(lay.config_path, "w", encoding="utf-8") as f:
        json.dump({"max_attempts": 5}, f)
    paths.init(str(tmp_path))  # second init must not clobber
    cfg = paths.load_config(lay)
    assert cfg["max_attempts"] == 5


def test_load_config_merges_defaults(lay):
    cfg = paths.load_config(lay)
    assert cfg["verifier_tier"] == "sonnet"
    assert cfg["lease_ttl_s"] == 5400
    assert cfg["max_chain_depth"] == 3
    assert cfg["max_attempts"] == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_paths.py -v`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError`.

- [ ] **Step 3: Implement `sb/paths.py`**

```python
"""Filesystem layout for a switchboard-enabled repo.

Transient orchestration state lives under .switchboard/ (gitignored).
Durable artifacts (decisions/, plans/) live at repo top level (tracked).
"""

import json
import os

LANES = ["queued", "active", "paused", "done", "failed"]

DEFAULT_CONFIG = {
    "schema_version": "0.1.0",
    "verifier_tier": "sonnet",
    "verifier_tier_fallback": "opus",
    "max_attempts": 3,
    "lease_ttl_s": 5400,
    "max_chain_depth": 3,
}


class Layout:
    def __init__(self, repo):
        self.repo = os.path.abspath(repo)
        self.root = os.path.join(self.repo, ".switchboard")
        self.tasks = os.path.join(self.root, "tasks")
        self.leases = os.path.join(self.root, "leases")
        self.heartbeats = os.path.join(self.root, "heartbeats")
        self.results = os.path.join(self.root, "results")
        self.config_path = os.path.join(self.root, "config.json")
        self.decisions = os.path.join(self.repo, "decisions")
        self.plans = os.path.join(self.repo, "plans")

    def lane(self, name):
        return os.path.join(self.tasks, name)


def init(repo):
    lay = Layout(repo)
    for lane in LANES:
        os.makedirs(lay.lane(lane), exist_ok=True)
    for d in [lay.leases, lay.heartbeats, lay.results, lay.decisions, lay.plans]:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(lay.config_path):
        with open(lay.config_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return lay


def load_config(lay):
    with open(lay.config_path, encoding="utf-8") as f:
        return {**DEFAULT_CONFIG, **json.load(f)}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_paths.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/paths.py tests/test_paths.py tests/conftest.py
git commit -m "feat(sb): layout, init, config with defaults merge"
```

---

### Task 4: Validation choke point

**Files:**
- Create: `sb/validate.py`
- Test: `tests/test_validate.py`, `tests/helpers.py`

- [ ] **Step 1: Write the failing test**

`tests/helpers.py`:
```python
"""Schema-valid task factory for tests. Every write goes through validation,
so fixtures must be complete."""


def make_task(task_id="PLAN-001/PH-1/T-1", tier="haiku", **over):
    plan_id, phase_id, leaf = task_id.split("/")[:3]
    task = {
        "schema_version": "0.2.0",
        "id": task_id,
        "tier": tier,
        "status": "queued",
        "source": {"plan_id": plan_id, "phase_id": phase_id,
                   "task_id": leaf.split(".")[0]},
        "goal": "do the thing",
        "context": {"repo_state": "HEAD", "branch": "sb/plan-001/ph-1",
                    "chain_depth": 0, "depends_on": []},
        "done": {"statement": "thing is done"},
        "attempts": 0,
        "created_at": "2026-06-12T00:00:00+00:00",
        "created_by": "test",
    }
    ctx = over.pop("context", None)
    task.update(over)
    if ctx:
        task["context"] = {**task["context"], **ctx}
    return task
```

`tests/test_validate.py`:
```python
import pytest

from sb import validate
from tests.helpers import make_task


def test_valid_task_passes():
    validate.check("task", make_task())


def test_invalid_task_raises_with_path():
    bad = make_task()
    bad["status"] = "no-such-status"
    with pytest.raises(ValueError, match="status"):
        validate.check("task", bad)


def test_result_validation():
    validate.check("result", {
        "schema_version": "0.1.0", "outcome": "success", "summary": "ok",
    })
    with pytest.raises(ValueError):
        validate.check("result", {"schema_version": "0.1.0", "outcome": "success"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_validate.py -v`
Expected: FAIL — `sb.validate` missing.

- [ ] **Step 3: Implement `sb/validate.py`**

```python
"""The single validation choke point. Every task/plan/result/decision write
in the engine calls check() before touching disk."""

import json
import os

from jsonschema import Draft202012Validator

_SCHEMA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "schemas")

NAMES = {
    "task": "task.schema.json",
    "plan": "plan.schema.json",
    "decision": "decision-record.schema.json",
    "result": "result.schema.json",
}

_cache = {}


def schema(name):
    if name not in _cache:
        with open(os.path.join(_SCHEMA_DIR, NAMES[name]), encoding="utf-8") as f:
            _cache[name] = Draft202012Validator(json.load(f))
    return _cache[name]


def check(name, obj):
    errors = sorted(schema(name).iter_errors(obj), key=lambda e: e.json_path)
    if errors:
        msgs = "; ".join(f"{e.json_path}: {e.message}" for e in errors[:5])
        raise ValueError(f"{name} schema validation failed: {msgs}")
    return obj
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_validate.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/validate.py tests/test_validate.py tests/helpers.py
git commit -m "feat(sb): jsonschema validation choke point"
```

---

### Task 5: Task store with atomic lane moves

**Files:**
- Create: `sb/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

`tests/test_store.py`:
```python
import pytest

from sb import store
from tests.helpers import make_task


def test_write_read_roundtrip(lay):
    t = make_task()
    store.write_task(lay, "queued", t)
    assert store.read_json(store.task_path(lay, "queued", t["id"])) == t


def test_write_task_validates(lay):
    bad = make_task()
    bad["status"] = "nope"
    with pytest.raises(ValueError):
        store.write_task(lay, "queued", bad)


def test_fname_escapes_slashes():
    assert store.fname("PLAN-001/PH-1/T-1") == "PLAN-001_PH-1_T-1.json"


def test_move_task_is_atomic_and_loses_race(lay):
    t = make_task()
    store.write_task(lay, "queued", t)
    assert store.move_task(lay, "queued", "active", t["id"]) is True
    # second mover finds the source gone — the lost race
    assert store.move_task(lay, "queued", "active", t["id"]) is False


def test_find_task_scans_lanes(lay):
    t = make_task()
    store.write_task(lay, "paused", t)
    lane, found = store.find_task(lay, t["id"])
    assert lane == "paused" and found["id"] == t["id"]
    assert store.find_task(lay, "PLAN-999/PH-9/T-9") == (None, None)


def test_list_tasks_and_done_ids(lay):
    a = make_task("PLAN-001/PH-1/T-1")
    b = make_task("PLAN-001/PH-1/T-2", status="done")
    store.write_task(lay, "queued", a)
    store.write_task(lay, "done", b)
    assert [t["id"] for t in store.list_tasks(lay, "queued")] == [a["id"]]
    assert store.done_ids(lay) == {b["id"]}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL — `sb.store` missing.

- [ ] **Step 3: Implement `sb/store.py`**

```python
"""Task file io. Lane transitions are atomic os.rename — the loser of a
claim race gets FileNotFoundError, never a corrupt state."""

import json
import os

from sb import validate
from sb.paths import LANES


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def fname(task_id):
    return task_id.replace("/", "_") + ".json"


def task_path(lay, lane, task_id):
    return os.path.join(lay.lane(lane), fname(task_id))


def write_task(lay, lane, task):
    validate.check("task", task)
    write_json(task_path(lay, lane, task["id"]), task)


def list_tasks(lay, lane):
    d = lay.lane(lane)
    out = []
    for f in sorted(os.listdir(d)):
        if f.endswith(".json"):
            out.append(read_json(os.path.join(d, f)))
    return out


def move_task(lay, src_lane, dst_lane, task_id):
    """Atomic lane transition. False = source already gone (lost a race)."""
    try:
        os.rename(task_path(lay, src_lane, task_id),
                  task_path(lay, dst_lane, task_id))
        return True
    except FileNotFoundError:
        return False


def find_task(lay, task_id):
    for lane in LANES:
        p = task_path(lay, lane, task_id)
        if os.path.exists(p):
            return lane, read_json(p)
    return None, None


def done_ids(lay):
    return {t["id"] for t in list_tasks(lay, "done")}
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/store.py tests/test_store.py
git commit -m "feat(sb): task store with validated writes and atomic lane moves"
```

---

### Task 6: Leases

**Files:**
- Create: `sb/leases.py`
- Test: `tests/test_leases.py`

- [ ] **Step 1: Write the failing test**

`tests/test_leases.py`:
```python
from sb import leases


def test_lease_roundtrip(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    lease = leases.read_lease(lay, "PLAN-001/PH-1/T-1")
    assert lease["worker_id"] == "worker-a"
    assert lease["ttl_s"] == 100


def test_missing_lease_reads_none(lay):
    assert leases.read_lease(lay, "PLAN-001/PH-1/T-9") is None


def test_expiry(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    lease = leases.read_lease(lay, "PLAN-001/PH-1/T-1")
    assert not leases.is_expired(lease, now=lease["claimed_at"] + 50)
    assert leases.is_expired(lease, now=lease["claimed_at"] + 101)


def test_clear_is_idempotent(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    leases.clear_lease(lay, "PLAN-001/PH-1/T-1")
    leases.clear_lease(lay, "PLAN-001/PH-1/T-1")  # no error
    assert leases.read_lease(lay, "PLAN-001/PH-1/T-1") is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_leases.py -v`
Expected: FAIL — `sb.leases` missing.

- [ ] **Step 3: Implement `sb/leases.py`**

```python
"""Claim leases. A stale lease means the claiming session died or stalled;
the task is requeued with attempts UNCHANGED (infra failure, not task failure)."""

import os
import time

from sb import store


def lease_path(lay, task_id):
    return os.path.join(lay.leases, store.fname(task_id))


def write_lease(lay, task_id, worker_id, ttl_s):
    store.write_json(lease_path(lay, task_id), {
        "task_id": task_id,
        "worker_id": worker_id,
        "claimed_at": time.time(),
        "ttl_s": ttl_s,
    })


def read_lease(lay, task_id):
    p = lease_path(lay, task_id)
    return store.read_json(p) if os.path.exists(p) else None


def is_expired(lease, now=None):
    now = time.time() if now is None else now
    return now > lease["claimed_at"] + lease["ttl_s"]


def clear_lease(lay, task_id):
    try:
        os.remove(lease_path(lay, task_id))
    except FileNotFoundError:
        pass
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_leases.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/leases.py tests/test_leases.py
git commit -m "feat(sb): claim leases with explicit-clock expiry"
```

---

### Task 7: Claims — deps, claim, blocking wait, stale requeue, heartbeat

**Files:**
- Create: `sb/claims.py`
- Test: `tests/test_claims.py`

- [ ] **Step 1: Write the failing test**

`tests/test_claims.py`:
```python
import os
import time

from sb import claims, leases, store
from tests.helpers import make_task


def seed(lay, *tasks, lane="queued"):
    for t in tasks:
        store.write_task(lay, lane, t)


def test_deps_met():
    t = make_task(context={"depends_on": ["A", "B"]})
    assert claims.deps_met(t, {"A", "B"})
    assert not claims.deps_met(t, {"A"})


def test_claim_respects_tier_and_deps(lay):
    seed(lay,
         make_task("PLAN-001/PH-1/T-1", tier="opus"),
         make_task("PLAN-001/PH-1/T-2", tier="haiku",
                   context={"depends_on": ["PLAN-001/PH-1/T-1"]}),
         make_task("PLAN-001/PH-1/T-3", tier="haiku"))
    got = claims.claim_one(lay, "w1", tier="haiku")
    assert got["id"] == "PLAN-001/PH-1/T-3"  # T-2 blocked, T-1 wrong tier


def test_claim_any_tier(lay):
    seed(lay, make_task("PLAN-001/PH-1/T-1", tier="opus"))
    got = claims.claim_one(lay, "w1")
    assert got["id"] == "PLAN-001/PH-1/T-1"


def test_claim_sets_state_and_lease_without_touching_attempts(lay):
    seed(lay, make_task())
    got = claims.claim_one(lay, "w1")
    assert got["status"] == "claimed"
    assert got["claim"]["worker_id"] == "w1"
    assert got["attempts"] == 0  # claims never count as attempts
    lane, on_disk = store.find_task(lay, got["id"])
    assert lane == "active"
    assert leases.read_lease(lay, got["id"])["worker_id"] == "w1"


def test_claim_returns_none_when_empty(lay):
    assert claims.claim_one(lay, "w1") is None


def test_claim_wait_returns_immediately_when_present(lay):
    seed(lay, make_task())
    start = time.monotonic()
    got = claims.claim_wait(lay, "w1", wait_s=5, poll_s=0.1)
    assert got is not None
    assert time.monotonic() - start < 1


def test_claim_wait_times_out(lay):
    start = time.monotonic()
    assert claims.claim_wait(lay, "w1", wait_s=0.3, poll_s=0.1) is None
    assert time.monotonic() - start >= 0.3


def test_requeue_stale_only_touches_expired(lay):
    fresh, stale = make_task("PLAN-001/PH-1/T-1"), make_task("PLAN-001/PH-1/T-2")
    seed(lay, fresh, stale)
    claims.claim_one(lay, "w1", )  # claims T-1
    claims.claim_one(lay, "w2")    # claims T-2
    # expire T-2's lease by rewriting it in the past
    lease = leases.read_lease(lay, stale["id"])
    lease["claimed_at"] -= lease["ttl_s"] + 1
    store.write_json(leases.lease_path(lay, stale["id"]), lease)

    requeued = claims.requeue_stale(lay, {"lease_ttl_s": 5400})
    assert requeued == [stale["id"]]
    lane, t = store.find_task(lay, stale["id"])
    assert lane == "queued" and t["status"] == "queued"
    assert "claim" not in t and t["attempts"] == 0
    assert store.find_task(lay, fresh["id"])[0] == "active"


def test_requeue_stale_handles_missing_lease(lay):
    seed(lay, make_task())
    got = claims.claim_one(lay, "w1")
    leases.clear_lease(lay, got["id"])
    assert claims.requeue_stale(lay, {}) == [got["id"]]


def test_heartbeat_touches_file(lay):
    claims.heartbeat(lay, "w1")
    p = os.path.join(lay.heartbeats, "w1.json")
    assert os.path.exists(p)
    assert store.read_json(p)["worker_id"] == "w1"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_claims.py -v`
Expected: FAIL — `sb.claims` missing.

- [ ] **Step 3: Implement `sb/claims.py`**

```python
"""Claiming, blocking wait, stale-lease requeue, heartbeats.

claim_wait blocks INSIDE the process (cheap polling against the local fs),
so a worker session pays one tool call per wait window, not per poll."""

import datetime as dt
import os
import time

from sb import leases, store


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def deps_met(task, completed):
    return all(d in completed for d in task.get("context", {}).get("depends_on", []))


def claimable(lay, tier=None):
    completed = store.done_ids(lay)
    out = []
    for t in store.list_tasks(lay, "queued"):
        if tier and t.get("tier") != tier:
            continue
        if deps_met(t, completed):
            out.append(t)
    return out


def claim_one(lay, worker_id, tier=None, cfg=None):
    ttl = (cfg or {}).get("lease_ttl_s", 5400)
    for t in claimable(lay, tier):
        if not store.move_task(lay, "queued", "active", t["id"]):
            continue  # lost the race; next candidate
        t["status"] = "claimed"
        t["claim"] = {"worker_id": worker_id, "claimed_at": now_iso()}
        store.write_task(lay, "active", t)
        leases.write_lease(lay, t["id"], worker_id, ttl)
        return t
    return None


def claim_wait(lay, worker_id, tier=None, cfg=None, wait_s=0, poll_s=0.5):
    deadline = time.monotonic() + wait_s
    while True:
        t = claim_one(lay, worker_id, tier, cfg)
        if t is not None or time.monotonic() >= deadline:
            return t
        time.sleep(poll_s)


def requeue_stale(lay, cfg):
    """Expired or missing lease => the claimer is gone. Infra failure:
    requeue with attempts UNCHANGED."""
    requeued = []
    for t in store.list_tasks(lay, "active"):
        lease = leases.read_lease(lay, t["id"])
        if lease is not None and not leases.is_expired(lease):
            continue
        if not store.move_task(lay, "active", "queued", t["id"]):
            continue
        t["status"] = "queued"
        t.pop("claim", None)
        store.write_task(lay, "queued", t)
        leases.clear_lease(lay, t["id"])
        requeued.append(t["id"])
    return requeued


def heartbeat(lay, worker_id):
    store.write_json(os.path.join(lay.heartbeats, f"{worker_id}.json"),
                     {"worker_id": worker_id, "at": time.time()})
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_claims.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/claims.py tests/test_claims.py
git commit -m "feat(sb): claims with blocking wait, stale-lease requeue, heartbeats"
```

---

### Task 8: DAG guards

**Files:**
- Create: `sb/dag.py`
- Test: `tests/test_dag.py`

- [ ] **Step 1: Write the failing test**

`tests/test_dag.py`:
```python
import pytest

from sb import dag, store
from tests.helpers import make_task


def test_acyclic_passes():
    dag.assert_acyclic({"A": ["B"], "B": ["C"], "C": []})


def test_cycle_raises():
    with pytest.raises(dag.CycleError):
        dag.assert_acyclic({"A": ["B"], "B": ["A"]})


def test_self_edge_raises():
    with pytest.raises(dag.CycleError):
        dag.assert_acyclic({"A": ["A"]})


def test_unknown_deps_are_leaves():
    dag.assert_acyclic({"A": ["DONE-ELSEWHERE"]})


def test_assert_addition_ok_catches_ancestor_cycle(lay):
    parent = make_task("PLAN-001/PH-1/T-1")
    store.write_task(lay, "active", parent)
    # a research task that (illegally) depends on its own parent, while the
    # parent will gain a dependency on it: A -> R -> A
    research = make_task("PLAN-001/PH-1/T-1.R1",
                         context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    with pytest.raises(dag.CycleError):
        dag.assert_addition_ok(lay, research,
                               extra_parent_deps=("PLAN-001/PH-1/T-1",
                                                  ["PLAN-001/PH-1/T-1.R1"]))


def test_assert_addition_ok_passes_clean_spawn(lay):
    parent = make_task("PLAN-001/PH-1/T-1")
    store.write_task(lay, "active", parent)
    research = make_task("PLAN-001/PH-1/T-1.R1")
    dag.assert_addition_ok(lay, research,
                           extra_parent_deps=("PLAN-001/PH-1/T-1",
                                              ["PLAN-001/PH-1/T-1.R1"]))
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_dag.py -v`
Expected: FAIL — `sb.dag` missing.

- [ ] **Step 3: Implement `sb/dag.py`**

```python
"""Dependency-graph guards. With released workers (no held resources),
an acyclic graph cannot deadlock; this is the enqueue-time check."""

from sb import store
from sb.paths import LANES


class CycleError(Exception):
    pass


def assert_acyclic(edge_map):
    state = {}

    def visit(node, path):
        st = state.get(node)
        if st == "done" or node not in edge_map:
            return  # unknown nodes are leaves (e.g. already-done tasks)
        if st == "visiting":
            raise CycleError(" -> ".join(path + [node]))
        state[node] = "visiting"
        for dep in edge_map[node]:
            visit(dep, path + [node])
        state[node] = "done"

    for node in list(edge_map):
        visit(node, [])


def all_edges(lay):
    edges = {}
    for lane in LANES:
        for t in store.list_tasks(lay, lane):
            edges[t["id"]] = list(t.get("context", {}).get("depends_on", []))
    return edges


def assert_addition_ok(lay, new_task, extra_parent_deps=None):
    """Validate the graph stays acyclic if new_task is enqueued (and the
    parent simultaneously gains extra deps, as in sb spawn)."""
    edges = all_edges(lay)
    edges[new_task["id"]] = list(new_task.get("context", {}).get("depends_on", []))
    if extra_parent_deps:
        parent_id, deps = extra_parent_deps
        edges[parent_id] = edges.get(parent_id, []) + list(deps)
    assert_acyclic(edges)
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_dag.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/dag.py tests/test_dag.py
git commit -m "feat(sb): enqueue-time DAG cycle guards"
```

---

### Task 9: Spawn — research handoff and parent continuation

**Files:**
- Create: `sb/spawn.py`
- Test: `tests/test_spawn.py`

- [ ] **Step 1: Write the failing test**

`tests/test_spawn.py`:
```python
import os

import pytest

from sb import claims, leases, spawn, store
from sb.paths import DEFAULT_CONFIG
from tests.helpers import make_task


def claimed_parent(lay, **over):
    t = make_task(**over)
    store.write_task(lay, "queued", t)
    return claims.claim_one(lay, "w1")


def test_spawn_creates_research_and_requeues_parent(lay):
    parent = claimed_parent(lay)
    research = spawn.spawn_research(
        lay, DEFAULT_CONFIG, parent["id"],
        goal="Benchmark both cache designs", tier="haiku",
        done_statement="A benchmark table exists comparing the designs.")
    assert research["id"] == f"{parent['id']}.R1"
    assert research["tier"] == "haiku"
    assert research["context"]["chain_depth"] == 1
    assert store.find_task(lay, research["id"])[0] == "queued"

    lane, p = store.find_task(lay, parent["id"])
    assert lane == "queued" and p["status"] == "queued"
    assert research["id"] in p["context"]["depends_on"]
    assert "claim" not in p
    assert leases.read_lease(lay, parent["id"]) is None


def test_spawn_carries_partial_result(lay):
    parent = claimed_parent(lay)
    rpath = os.path.join(lay.results, store.fname(parent["id"]))
    store.write_json(rpath, {"schema_version": "0.1.0", "outcome": "blocked",
                             "summary": "Need benchmark data before deciding."})
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g", tier="haiku", done_statement="d")
    _, p = store.find_task(lay, parent["id"])
    assert p["context"]["prior_attempts"][0]["summary"].startswith("Need benchmark")
    assert not os.path.exists(rpath)  # consumed


def test_spawn_suffix_increments(lay):
    parent = claimed_parent(lay)
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g1", tier="haiku", done_statement="d")
    # parent went back to queued; clear its dep so it can be claimed again
    _, p = store.find_task(lay, parent["id"])
    p["context"]["depends_on"] = []
    store.write_task(lay, "queued", p)
    claims.claim_one(lay, "w1", tier="haiku")  # claims R1 (sorts first)
    claims.claim_one(lay, "w1", tier="haiku")  # claims the parent
    r2 = spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                              goal="g2", tier="haiku", done_statement="d")
    assert r2["id"].endswith(".R2")


def test_spawn_rejects_queued_continuation_parent(lay):
    parent = claimed_parent(lay)
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g1", tier="haiku", done_statement="d")
    # parent is queued as a continuation; spawning from it must fail —
    # only the active claimer owns the right to spawn
    with pytest.raises(ValueError, match="not active"):
        spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                             goal="g2", tier="haiku", done_statement="d")


def test_spawn_depth_cap_pauses_for_human(lay):
    parent = claimed_parent(lay, context={"chain_depth": 3})
    out = spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                               goal="g", tier="haiku", done_statement="d")
    assert out is None
    lane, p = store.find_task(lay, parent["id"])
    assert lane == "paused" and p["status"] == "paused_for_human"
    assert "chain depth" in p["failure"]["reason"]


def test_spawn_requires_active_parent(lay):
    t = make_task()
    store.write_task(lay, "queued", t)
    with pytest.raises(ValueError, match="not active"):
        spawn.spawn_research(lay, DEFAULT_CONFIG, t["id"],
                             goal="g", tier="haiku", done_statement="d")
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_spawn.py -v`
Expected: FAIL — `sb.spawn` missing.

- [ ] **Step 3: Implement `sb/spawn.py`**

```python
"""Research handoff. A waiting parent NEVER holds a worker: the parent is
re-enqueued as its own continuation, gaining a dependency on the research
task and carrying its partial result forward (retries are never blind)."""

import datetime as dt
import os

from sb import dag, leases, store, validate
from sb.paths import LANES


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _next_suffix(lay, parent_id, marker):
    prefix = f"{parent_id}.{marker}"
    n = 0
    for lane in LANES:
        for t in store.list_tasks(lay, lane):
            tail = t["id"][len(prefix):] if t["id"].startswith(prefix) else ""
            if tail.isdigit():
                n = max(n, int(tail))
    return n + 1


def spawn_research(lay, cfg, parent_id, goal, tier, done_statement):
    lane, parent = store.find_task(lay, parent_id)
    if lane != "active":
        raise ValueError(f"{parent_id} is not active (lane={lane})")

    depth = parent.get("context", {}).get("chain_depth", 0) + 1
    if depth > cfg.get("max_chain_depth", 3):
        parent["status"] = "paused_for_human"
        parent["failure"] = {
            "reason": f"chain depth {depth} exceeds max "
                      f"{cfg.get('max_chain_depth', 3)}; human review required"}
        # write-before-move invariant (see claims.requeue_stale)
        store.write_task(lay, "active", parent)
        store.move_task(lay, "active", "paused", parent_id)
        leases.clear_lease(lay, parent_id)
        return None

    rid = f"{parent_id}.R{_next_suffix(lay, parent_id, 'R')}"
    research = {
        "schema_version": "0.2.0",
        "id": rid,
        "tier": tier,
        "status": "queued",
        "source": parent.get("source", {}),
        "goal": goal,
        "context": {
            "repo_state": parent.get("context", {}).get("repo_state", "HEAD"),
            "branch": parent.get("context", {}).get("branch", ""),
            "chain_depth": depth,
            "depends_on": [],
        },
        "done": {"statement": done_statement},
        "attempts": 0,
        "created_at": now_iso(),
        "created_by": parent_id,
    }
    validate.check("task", research)
    dag.assert_addition_ok(lay, research, extra_parent_deps=(parent_id, [rid]))
    store.write_task(lay, "queued", research)

    # consume the parent's partial result, if the session wrote one
    rpath = os.path.join(lay.results, store.fname(parent_id))
    if os.path.exists(rpath):
        partial = store.read_json(rpath)
        parent.setdefault("context", {}).setdefault("prior_attempts", []).append(partial)
        os.remove(rpath)

    parent["context"].setdefault("depends_on", []).append(rid)
    parent["status"] = "queued"
    parent.pop("claim", None)
    parent.pop("result", None)
    # write-before-move invariant: body finalized while still in active/
    # (un-claimable), then renamed. Never write after a move into queued/.
    store.write_task(lay, "active", parent)
    store.move_task(lay, "active", "queued", parent_id)
    leases.clear_lease(lay, parent_id)
    return research
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_spawn.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/spawn.py tests/test_spawn.py
git commit -m "feat(sb): research spawn with parent continuation, depth cap, cycle guard"
```

---

### Task 10: file-result — outcome routing and the verification lane

**Files:**
- Create: `sb/results.py`
- Test: `tests/test_results.py`

- [ ] **Step 1: Write the failing test**

`tests/test_results.py`:
```python
import os

import pytest

from sb import claims, results, store
from sb.paths import DEFAULT_CONFIG
from tests.helpers import make_task


def active_task(lay, **over):
    t = make_task(**over)
    store.write_task(lay, "queued", t)
    return claims.claim_one(lay, "w1")


def write_result(lay, task_id, **fields):
    r = {"schema_version": "0.1.0", "outcome": "success", "summary": "done", **fields}
    store.write_json(os.path.join(lay.results, store.fname(task_id)), r)


def test_success_awaits_verification_and_enqueues_verify_task(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "paused"
    lane, on_disk = store.find_task(lay, t["id"])
    assert lane == "paused" and on_disk["status"] == "awaiting_verification"
    assert on_disk["result"]["outcome"] == "success"

    vlane, vtask = store.find_task(lay, f"{t['id']}.V1")
    assert vlane == "queued"
    assert vtask["context"]["verifies"] == t["id"]
    assert vtask["tier"] == "sonnet"  # author opus -> configured verifier


def test_verifier_tier_falls_back_when_author_matches(lay):
    t = active_task(lay, tier="sonnet")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    _, vtask = store.find_task(lay, f"{t['id']}.V1")
    assert vtask["tier"] == "opus"


def test_verification_tasks_are_not_reverified(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2", tier="sonnet")
    write_result(lay, v["id"], verdict="pass")
    dest = results.file_result(lay, DEFAULT_CONFIG, v["id"])
    assert dest == "done"
    assert store.find_task(lay, f"{v['id']}.V1") == (None, None)


def test_verdict_pass_moves_target_done(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2", tier="sonnet")
    write_result(lay, v["id"], verdict="pass")
    results.file_result(lay, DEFAULT_CONFIG, v["id"])
    lane, target = store.find_task(lay, t["id"])
    assert lane == "done" and target["status"] == "done"


def test_verdict_fail_requeues_target_with_notes(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2", tier="sonnet")
    write_result(lay, v["id"], verdict="fail", verdict_notes="stress test fails")
    results.file_result(lay, DEFAULT_CONFIG, v["id"])
    lane, target = store.find_task(lay, t["id"])
    assert lane == "queued" and target["attempts"] == 1
    prior = target["context"]["prior_attempts"][0]
    assert prior["verifier_notes"] == "stress test fails"
    assert "result" not in target


def test_verdict_fail_at_max_attempts_fails_target(lay):
    t = active_task(lay)
    _, on_disk = store.find_task(lay, t["id"])
    on_disk["attempts"] = 2  # one more failure hits max_attempts=3
    store.write_task(lay, "active", on_disk)
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2")
    write_result(lay, v["id"], verdict="fail", verdict_notes="still broken")
    results.file_result(lay, DEFAULT_CONFIG, v["id"])
    lane, target = store.find_task(lay, t["id"])
    assert lane == "failed" and target["status"] == "failed"


def test_blocked_pauses_for_human(lay):
    t = active_task(lay)
    write_result(lay, t["id"], outcome="blocked", summary="missing credential")
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "paused"
    assert store.find_task(lay, t["id"])[1]["status"] == "paused_for_human"


def test_partial_requeues_and_increments_attempts(lay):
    t = active_task(lay)
    write_result(lay, t["id"], outcome="partial", summary="half done")
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "queued"
    _, on_disk = store.find_task(lay, t["id"])
    assert on_disk["attempts"] == 1
    assert on_disk["context"]["prior_attempts"][0]["summary"] == "half done"


def test_missing_result_file_raises(lay):
    t = active_task(lay)
    with pytest.raises(FileNotFoundError):
        results.file_result(lay, DEFAULT_CONFIG, t["id"])


def test_invalid_result_rejected(lay):
    t = active_task(lay)
    store.write_json(os.path.join(lay.results, store.fname(t["id"])),
                     {"schema_version": "0.1.0", "outcome": "success"})
    with pytest.raises(ValueError):
        results.file_result(lay, DEFAULT_CONFIG, t["id"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_results.py -v`
Expected: FAIL — `sb.results` missing.

- [ ] **Step 3: Implement `sb/results.py`**

```python
"""file-result: the only door results enter through. Routes by outcome,
enqueues the verification lane (PHI-030: only a verifier verdict reaches
done), and applies verdicts back to targets."""

import datetime as dt
import os

from sb import leases, store, validate


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def result_path(lay, task_id):
    return os.path.join(lay.results, store.fname(task_id))


def verifier_tier_for(author_tier, cfg):
    vt = cfg.get("verifier_tier", "sonnet")
    return cfg.get("verifier_tier_fallback", "opus") if vt == author_tier else vt


def file_result(lay, cfg, task_id):
    rp = result_path(lay, task_id)
    if not os.path.exists(rp):
        raise FileNotFoundError(f"no result file at {rp}")
    result = validate.check("result", store.read_json(rp))
    lane, task = store.find_task(lay, task_id)
    if lane != "active":
        raise ValueError(f"{task_id} is not active (lane={lane})")
    result.setdefault("completed_at", now_iso())
    task["result"] = result

    target_id = task.get("context", {}).get("verifies")
    if target_id:
        dest = _apply_verdict(lay, cfg, task, result, target_id)
    else:
        dest = _route_outcome(lay, cfg, task, result)
    # Body finalized BEFORE the rename: once a file lands in queued/ it is
    # instantly claimable, so no write may follow the move (ghost-task race —
    # same invariant as claims.requeue_stale).
    store.write_task(lay, "active", task)
    if not store.move_task(lay, "active", dest, task_id):
        raise ValueError(f"{task_id} vanished from active while filing (swept?)")
    leases.clear_lease(lay, task_id)
    os.remove(rp)
    return dest


def _route_outcome(lay, cfg, task, result):
    outcome = result["outcome"]
    if outcome == "success":
        task["status"] = "awaiting_verification"
        _enqueue_verification(lay, cfg, task)
        return "paused"
    if outcome == "blocked":
        task["status"] = "paused_for_human"
        return "paused"
    return _requeue_or_fail(lay, cfg, task, f"outcome={outcome}")


def _requeue_or_fail(lay, cfg, task, note):
    task.setdefault("context", {}).setdefault("prior_attempts", []) \
        .append(task.pop("result"))
    task["attempts"] = task.get("attempts", 0) + 1
    task.pop("claim", None)
    if task["attempts"] >= cfg.get("max_attempts", 3):
        task["status"] = "failed"
        task["failure"] = {"reason": note}
        return "failed"
    task["status"] = "queued"
    return "queued"


def _enqueue_verification(lay, cfg, task):
    vid = f"{task['id']}.V{task.get('attempts', 0) + 1}"
    verify = {
        "schema_version": "0.2.0",
        "id": vid,
        "tier": verifier_tier_for(task.get("tier"), cfg),
        "status": "queued",
        "source": task.get("source", {}),
        "goal": f"Verify: {task['goal']}",
        "context": {
            "repo_state": task.get("context", {}).get("repo_state", "HEAD"),
            "branch": task.get("context", {}).get("branch", ""),
            "chain_depth": task.get("context", {}).get("chain_depth", 0),
            "verifies": task["id"],
            "depends_on": [],
        },
        "done": task["done"],
        "attempts": 0,
        "created_at": now_iso(),
        "created_by": "sb",
    }
    store.write_task(lay, "queued", verify)


def _apply_verdict(lay, cfg, vtask, result, target_id):
    verdict = result.get("verdict")
    if verdict not in ("pass", "fail"):
        raise ValueError("verification result must set verdict: pass|fail")
    lane, target = store.find_task(lay, target_id)
    if lane != "paused" or target.get("status") != "awaiting_verification":
        raise ValueError(f"{target_id} is not awaiting verification (lane={lane})")

    if verdict == "pass":
        target["status"] = "done"
        dest = "done"
    else:
        prior = target.pop("result", None) or {}
        prior["verifier_notes"] = result.get("verdict_notes", "verification failed")
        target.setdefault("context", {}).setdefault("prior_attempts", []).append(prior)
        target["attempts"] = target.get("attempts", 0) + 1
        target.pop("claim", None)
        if target["attempts"] >= cfg.get("max_attempts", 3):
            target["status"] = "failed"
            target["failure"] = {"reason": f"verification failed: "
                                           f"{prior['verifier_notes']}"}
            dest = "failed"
        else:
            target["status"] = "queued"
            dest = "queued"
    # Same write-before-move invariant: update the body while the target is
    # still in paused/ (un-claimable), then rename. Never write after a move.
    store.write_task(lay, "paused", target)
    if not store.move_task(lay, "paused", dest, target_id):
        raise ValueError(f"{target_id} vanished from paused while applying verdict")
    vtask["status"] = "done"
    return "done"  # the verification task itself always completes
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_results.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/results.py tests/test_results.py
git commit -m "feat(sb): file-result routing and verification lane"
```

---

### Task 11: Seed — plan → queue with branches and gate-per-phase

**Files:**
- Create: `sb/seed.py`
- Test: `tests/test_seed.py`

- [ ] **Step 1: Write the failing test**

`tests/test_seed.py`:
```python
import pytest

from sb import seed, store

PLAN = {
    "schema_version": "0.1.0",
    "plan_id": "PLAN-001",
    "goal": "toy goal",
    "created": "2026-06-12T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "constraints": ["no new deps"],
    "grounding": ["HDR-006"],
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [
             {"task_id": "T-1", "title": "Choose the design",
              "done": {"statement": "ADR exists"}},
         ]},
        {"phase_id": "PH-2", "name": "Build", "default_model": "haiku",
         "tasks": [
             {"task_id": "T-2", "title": "Implement it",
              "depends_on": ["T-1"],
              "done": {"statement": "tests green"}},
         ]},
    ],
}


def test_seed_creates_tasks_with_branch_and_grounding(lay):
    seeded = seed.seed(lay, PLAN, repo_state="abc123")
    assert "PLAN-001/PH-1/T-1" in seeded
    _, t1 = store.find_task(lay, "PLAN-001/PH-1/T-1")
    assert t1["tier"] == "opus"
    assert t1["context"]["branch"] == "sb/plan-001/ph-1"
    assert t1["context"]["repo_state"] == "abc123"
    assert t1["context"]["grounding"] == ["HDR-006"]
    assert t1["context"]["chain_depth"] == 0


def test_every_phase_gets_a_gate_and_next_phase_blocks_on_it(lay):
    seed.seed(lay, PLAN)
    lane, gate1 = store.find_task(lay, "PLAN-001/PH-1/GATE")
    assert lane == "paused" and gate1["status"] == "paused_for_human"
    assert gate1["context"]["depends_on"] == ["PLAN-001/PH-1/T-1"]

    _, t2 = store.find_task(lay, "PLAN-001/PH-2/T-2")
    assert set(t2["context"]["depends_on"]) == {
        "PLAN-001/PH-1/T-1", "PLAN-001/PH-1/GATE"}

    lane, gate2 = store.find_task(lay, "PLAN-001/PH-2/GATE")
    assert lane == "paused"  # final phase is gated too: the PR-gate invariant


def test_blocking_questions_hold_seed(lay):
    plan = dict(PLAN, open_questions=[
        {"question": "Which SLA?", "blocking": True}])
    with pytest.raises(seed.BlockingQuestions, match="Which SLA"):
        seed.seed(lay, plan)
    assert store.list_tasks(lay, "queued") == []


def test_force_overrides_blocking_questions(lay):
    plan = dict(PLAN, open_questions=[
        {"question": "Which SLA?", "blocking": True}])
    seeded = seed.seed(lay, plan, force=True)
    assert len(seeded) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_seed.py -v`
Expected: FAIL — `sb.seed` missing.

- [ ] **Step 3: Implement `sb/seed.py`**

```python
"""Plan -> queue expansion. v2 changes from bootstrap.py: branch per phase,
chain_depth seeded, EVERY phase ends at a gate (the PR-gate invariant), no
git operations, schema validation throughout."""

import datetime as dt

from sb import store, validate


class BlockingQuestions(Exception):
    pass


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def composite(plan_id, phase_id, task_id):
    return f"{plan_id}/{phase_id}/{task_id}"


def seed(lay, plan, repo_state="HEAD", force=False):
    validate.check("plan", plan)
    blocking = [q["question"] for q in plan.get("open_questions", [])
                if q.get("blocking")]
    if blocking and not force:
        raise BlockingQuestions("; ".join(blocking))

    plan_id = plan["plan_id"]
    author = plan.get("author", {}).get("id", "unknown")
    where = {t["task_id"]: ph["phase_id"]
             for ph in plan["phases"] for t in ph.get("tasks", [])}
    seeded = []
    prev_gate = None

    for ph in plan["phases"]:
        branch = f"sb/{plan_id}/{ph['phase_id']}".lower()
        phase_cids = []
        for t in ph.get("tasks", []):
            cid = composite(plan_id, ph["phase_id"], t["task_id"])
            deps = [composite(plan_id, where[d], d)
                    for d in t.get("depends_on", []) if d in where]
            if prev_gate:
                deps.append(prev_gate)
            task = {
                "schema_version": "0.2.0",
                "id": cid,
                "tier": t.get("model") or ph["default_model"],
                "status": "queued",
                "source": {"plan_id": plan_id, "phase_id": ph["phase_id"],
                           "task_id": t["task_id"]},
                "goal": t["title"],
                "context": {
                    "repo_state": repo_state,
                    "branch": branch,
                    "chain_depth": 0,
                    "grounding": plan.get("grounding", []),
                    "constraints": plan.get("constraints", []),
                    "depends_on": deps,
                },
                "done": t["done"],
                "attempts": 0,
                "created_at": now_iso(),
                "created_by": author,
            }
            if t.get("budget") or ph.get("budget"):
                task["budget"] = t.get("budget") or ph["budget"]
            store.write_task(lay, "queued", task)
            seeded.append(cid)
            phase_cids.append(cid)

        # PR-gate invariant: every phase ends at a gate, only sb stamp
        # (Plan 2) completes it.
        gate_cid = composite(plan_id, ph["phase_id"], "GATE")
        gate = {
            "schema_version": "0.2.0",
            "id": gate_cid,
            "tier": "fable",
            "status": "paused_for_human",
            "source": {"plan_id": plan_id, "phase_id": ph["phase_id"],
                       "task_id": "GATE"},
            "goal": f"Human review gate: {ph['name']}",
            "context": {"repo_state": repo_state, "branch": branch,
                        "chain_depth": 0, "depends_on": phase_cids},
            "done": {"statement": ph.get("gate", {}).get(
                "condition", "phase PR merged")},
            "attempts": 0,
            "created_at": now_iso(),
            "created_by": author,
        }
        store.write_task(lay, "paused", gate)
        prev_gate = gate_cid
    return seeded
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_seed.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/seed.py tests/test_seed.py
git commit -m "feat(sb): plan seeding with phase branches and PR-gate invariant"
```

---

### Task 12: Decision query

**Files:**
- Create: `sb/query.py`
- Test: `tests/test_query.py`

- [ ] **Step 1: Write the failing test**

`tests/test_query.py`:
```python
import json
import os

from sb import query


def put(lay, rec):
    with open(os.path.join(lay.decisions, f"{rec['id']}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f)


def rec(rid, title, tags, status="approved", superseded_by=None):
    r = {"schema_version": "0.3.0", "id": rid, "type": "human",
         "status": status, "timestamp": "2026-06-12T00:00:00+00:00",
         "title": title, "tags": tags,
         "author": {"kind": "human", "id": "colin"},
         "reasoning": f"reasoning for {title}"}
    if superseded_by:
        r["superseded_by"] = superseded_by
    return r


def test_query_ranks_tag_matches_first(lay):
    put(lay, rec("HDR-101", "Pick a cache design", ["caching", "concurrency"]))
    put(lay, rec("HDR-102", "Name the framework", ["naming"]))
    out = query.query(lay, tags=["caching"], limit=5)
    assert out[0]["id"] == "HDR-101"


def test_query_text_keywords(lay):
    put(lay, rec("HDR-101", "Pick a cache design", []))
    put(lay, rec("HDR-102", "Name the framework", []))
    out = query.query(lay, text="how should the cache work", limit=5)
    assert out and out[0]["id"] == "HDR-101"


def test_superseded_excluded_by_default(lay):
    put(lay, rec("HDR-101", "Old way", ["caching"], superseded_by="HDR-102"))
    put(lay, rec("HDR-102", "New way", ["caching"]))
    ids = [d["id"] for d in query.query(lay, tags=["caching"])]
    assert ids == ["HDR-102"]
    ids = [d["id"] for d in query.query(lay, tags=["caching"],
                                        include_superseded=True)]
    assert set(ids) == {"HDR-101", "HDR-102"}


def test_digest_shape_is_lean(lay):
    put(lay, rec("HDR-101", "Pick a cache design", ["caching"]))
    d = query.query(lay, tags=["caching"])[0]
    assert set(d) == {"id", "type", "title", "chosen", "status", "tags",
                      "timestamp", "reasoning", "evidence", "superseded_by"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_query.py -v`
Expected: FAIL — `sb.query` missing.

- [ ] **Step 3: Implement `sb/query.py`**

```python
"""Zero-token precedent retrieval over the tracked decisions/ directory.
Keyword ranking for now; embeddings are an explicit deferral (spec §10)."""

import os
import re

from sb import store

STOP = {"the", "a", "an", "to", "of", "for", "and", "or", "in", "on", "with",
        "that", "add", "build", "make", "use", "it", "is", "be", "we", "our",
        "this", "how", "should", "work"}


def _keywords(text):
    return {w for w in re.findall(r"[a-z0-9-]+", (text or "").lower())
            if w not in STOP and len(w) > 2}


def _load(lay):
    out = []
    if not os.path.isdir(lay.decisions):
        return out
    for f in sorted(os.listdir(lay.decisions)):
        if f.endswith(".json"):
            try:
                out.append(store.read_json(os.path.join(lay.decisions, f)))
            except Exception:
                continue
    return out


def _digest(d):
    return {
        "id": d.get("id"),
        "type": d.get("type"),
        "title": d.get("title"),
        "chosen": d.get("chosen"),
        "status": d.get("status"),
        "tags": d.get("tags", []),
        "timestamp": d.get("timestamp"),
        "reasoning": (d.get("reasoning") or "")[:240],
        "evidence": [e.get("ref") for e in d.get("evidence", [])],
        "superseded_by": d.get("superseded_by"),
    }


def query(lay, tags=None, level=None, status=None, text=None, limit=8,
          include_superseded=False):
    want_tags = set(tags or [])
    want_words = _keywords(text)
    scored = []
    for d in _load(lay):
        if d.get("superseded_by") and not include_superseded:
            continue
        if level and d.get("level") != level:
            continue
        if status and d.get("status") != status:
            continue
        score = 3 * len(want_tags & set(d.get("tags", [])))
        body = " ".join([d.get("title", ""), d.get("context", ""),
                         d.get("reasoning", "")])
        score += len(want_words & _keywords(body))
        if score > 0 or not (want_tags or want_words):
            scored.append((score, d.get("id", ""), d))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [_digest(d) for _, _, d in scored[:limit]]
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_query.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/query.py tests/test_query.py
git commit -m "feat(sb): decision query over tracked decisions/"
```

---

### Task 13: CLI wiring and end-to-end smoke

**Files:**
- Create: `sb/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:
```python
import json
import os

import pytest

from sb import cli, store
from sb.paths import Layout
from tests.test_seed import PLAN


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out else None


def write_result(lay, task_id, **fields):
    r = {"schema_version": "0.1.0", "outcome": "success", "summary": "ok",
         **fields}
    store.write_json(os.path.join(lay.results, store.fname(task_id)), r)


def test_full_pipeline_through_cli(tmp_path, capsys):
    repo = str(tmp_path)
    lay = Layout(repo)

    assert cli.main(["init", "--repo", repo]) == 0
    capsys.readouterr()

    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(PLAN, f)
    code, seeded = run(capsys, "seed", "--repo", repo, "--plan", plan_path)
    assert code == 0 and len(seeded["seeded"]) == 2

    # claim PH-1's task, file success, verify it, pass the verdict
    code, task = run(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    assert code == 0 and task["id"] == "PLAN-001/PH-1/T-1"

    write_result(lay, task["id"])
    code, out = run(capsys, "file-result", task["id"], "--repo", repo)
    assert code == 0 and out["lane"] == "paused"

    code, vtask = run(capsys, "claim", "--repo", repo, "--worker-id", "w2")
    assert vtask["context"]["verifies"] == task["id"]
    write_result(lay, vtask["id"], verdict="pass")
    run(capsys, "file-result", vtask["id"], "--repo", repo)
    assert store.find_task(lay, task["id"])[0] == "done"

    # PH-2 stays blocked behind the un-stamped PH-1 gate
    code, nothing = run(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    assert code == 3 and nothing is None


def test_claim_exit_code_when_empty(tmp_path, capsys):
    repo = str(tmp_path)
    cli.main(["init", "--repo", repo])
    capsys.readouterr()
    assert cli.main(["claim", "--repo", repo, "--worker-id", "w1"]) == 3


def test_spawn_via_cli(tmp_path, capsys):
    repo = str(tmp_path)
    lay = Layout(repo)
    cli.main(["init", "--repo", repo])
    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(PLAN, f)
    cli.main(["seed", "--repo", repo, "--plan", plan_path])
    capsys.readouterr()
    code, task = run(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    code, research = run(capsys, "spawn", "--repo", repo, "--task", task["id"],
                         "--goal", "research it", "--tier", "haiku",
                         "--done", "research summary exists")
    assert code == 0 and research["id"] == f"{task['id']}.R1"


def test_seed_blocked_questions_exit_code(tmp_path, capsys):
    repo = str(tmp_path)
    cli.main(["init", "--repo", repo])
    plan = dict(PLAN, open_questions=[{"question": "SLA?", "blocking": True}])
    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    capsys.readouterr()
    assert cli.main(["seed", "--repo", repo, "--plan", plan_path]) == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL — `sb.cli` missing.

- [ ] **Step 3: Implement `sb/cli.py`**

```python
"""The sb command. Machine-first: JSON on stdout, exit codes for control
flow (0 ok, 2 held/blocked, 3 nothing to claim). Skills consume this."""

import argparse
import json
import sys

from sb import claims, paths, query, results, seed, spawn, store


def _out(obj):
    print(json.dumps(obj, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sb")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", default=".")
        return p

    common(sub.add_parser("init"))

    p = common(sub.add_parser("claim"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--tier", default=None)
    p.add_argument("--wait", type=float, default=0)

    p = common(sub.add_parser("file-result"))
    p.add_argument("task_id")

    p = common(sub.add_parser("spawn"))
    p.add_argument("--task", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--tier", required=True)
    p.add_argument("--done", required=True)

    p = common(sub.add_parser("seed"))
    p.add_argument("--plan", required=True)
    p.add_argument("--repo-state", default="HEAD")
    p.add_argument("--force", action="store_true")

    common(sub.add_parser("requeue-stale"))

    p = common(sub.add_parser("query"))
    p.add_argument("--text", default=None)
    p.add_argument("--tags", default="")
    p.add_argument("--limit", type=int, default=8)

    p = common(sub.add_parser("heartbeat"))
    p.add_argument("--worker-id", required=True)

    a = ap.parse_args(argv)

    if a.cmd == "init":
        lay = paths.init(a.repo)
        _out({"initialized": lay.root})
        return 0

    lay = paths.Layout(a.repo)
    cfg = paths.load_config(lay)

    if a.cmd == "claim":
        task = claims.claim_wait(lay, a.worker_id, tier=a.tier, cfg=cfg,
                                 wait_s=a.wait)
        if task is None:
            return 3
        _out(task)
        return 0

    if a.cmd == "file-result":
        lane = results.file_result(lay, cfg, a.task_id)
        _out({"task_id": a.task_id, "lane": lane})
        return 0

    if a.cmd == "spawn":
        research = spawn.spawn_research(lay, cfg, a.task, goal=a.goal,
                                        tier=a.tier, done_statement=a.done)
        if research is None:
            _out({"spawned": None, "reason": "chain depth cap; parent paused"})
            return 2
        _out(research)
        return 0

    if a.cmd == "seed":
        plan = store.read_json(a.plan)
        try:
            seeded = seed.seed(lay, plan, repo_state=a.repo_state,
                               force=a.force)
        except seed.BlockingQuestions as e:
            print(json.dumps({"held": str(e)}), file=sys.stderr)
            return 2
        _out({"seeded": seeded})
        return 0

    if a.cmd == "requeue-stale":
        _out({"requeued": claims.requeue_stale(lay, cfg)})
        return 0

    if a.cmd == "query":
        tags = [t for t in a.tags.split(",") if t]
        _out(query.query(lay, tags=tags, text=a.text, limit=a.limit))
        return 0

    if a.cmd == "heartbeat":
        claims.heartbeat(lay, a.worker_id)
        _out({"heartbeat": a.worker_id})
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify it passes, then run the whole suite**

Run: `.venv/bin/pytest tests/test_cli.py -v && .venv/bin/pytest -q`
Expected: 4 passed; full suite green.

- [ ] **Step 5: Commit**

```bash
git add sb/cli.py tests/test_cli.py
git commit -m "feat(sb): CLI wiring with end-to-end pipeline smoke test"
```

---

### Task 14: Retire superseded v1 modules

**Files:**
- Delete: `worker.py`, `bootstrap.py`, `query_decisions.py`
- Modify: `README.md` (quickstart section only)

- [ ] **Step 1: Delete the superseded harness**

```bash
git rm worker.py bootstrap.py query_decisions.py
```

`gate.py` and `rabbit_guard.py` stay until Plans 2 and 3 replace them
(`sb stamp/brief` and the hook rewrite respectively) — delete only what this
plan has actually replaced.

- [ ] **Step 2: Replace README quickstart**

Replace the `## Quickstart — run the demo with no model wired` section's
command block with:

```bash
pip install -e '.[dev]' && pytest          # the engine is fully unit-tested
sb init --repo .                            # scaffold .switchboard/
sb seed --repo . --plan plans/PLAN-031.json --force
sb claim --repo . --worker-id me            # JSON task on stdout; exit 3 = empty
```

Add one line under the header noting: "v1's worker.py/bootstrap.py are
superseded by the sb engine — see docs/specs/2026-06-12-switchboard-v2-design.md."

- [ ] **Step 3: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore: retire v1 worker/bootstrap/query_decisions, point README at sb engine"
```

---

## Self-review results

- **Spec coverage (Plan 1 scope):** §2 contracts/engine split ✓ (Tasks 2, 4); §3.1 claim/wait/heartbeat ✓ (Task 7); §3.2 prior-attempt carry ✓ (Tasks 9, 10); §3.3 spawn/continuation/depth/cycle ✓ (Tasks 8, 9); §4.1 lanes/leases/stale-requeue ✓ (Tasks 5–7); §4.3 branch fields ✓ (Task 11); §5.2 gate placeholders ✓ (Task 11, completion arrives with `sb stamp` in Plan 2); §6 verification lane ✓ (Task 10); §8 attempts-untouched-on-infra-failure ✓ (Task 7). Deliberately deferred: brief/digest/stamp/notify (Plan 2), skill/protocols/hooks/e2e-with-models (Plan 3).
- **Placeholder scan:** none — every step carries complete code or exact edits.
- **Type consistency:** `Layout`/`lay`, `cfg` dict, task dict shape, and `(lane, task)` tuple returns are uniform across modules; `file_result` returns the destination lane string everywhere it's asserted.

## PLAN-031 note

The example plan `plans/PLAN-031.json` declares `schema_version: 0.1.0` and remains valid against the plan schema (unchanged this plan). Task files it seeds are created fresh at `0.2.0` by `sb seed`. The old example artifacts in `examples/` and `.decisions/` demo records are cleaned up in Plan 2 when `sb brief/stamp` replace `gate.py`.
