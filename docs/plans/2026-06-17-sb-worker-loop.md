# sb Worker Loop + Subagent Protocols Implementation Plan (M0, Plan 3 sub-plan A)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the execution spine of the M0 judgment layer — the `/sb-work` worker loop and the **task**/**verifier** subagent prompt protocols, plus the one engine verb the loop needs (`sb release`) and a token-free loop-diagnostic helper — per the approved spec (`docs/specs/2026-06-16-sb-worker-loop-design.md`).

**Architecture:** The loop is a Claude Code skill (`.claude/skills/sb-work/`) that never does task work in its own context: it claims a task, provisions an isolated git worktree off the phase branch, dispatches a **fresh-context subagent** at the task's tier (model override from `tiers.json`), files the validated result through `sb file-result`, and tears the worktree down. The `sb` engine stays git-free and deterministic; the skill owns all git. Two small Python additions are TDD'd: `sb release` (infra-requeue a specific active task with attempts unchanged) and `sb/loopledger.py` (token-free per-iteration ledger + the productive-vs-churn diagnostic computed at the loop checkpoint). The prompt protocols are prose, reviewed against the spec — they get green tests only indirectly, via the exit bar (D).

**Tech Stack:** Python 3.11+, `jsonschema`, `pytest`, `git worktree`. No new dependencies. Skill prose is Markdown with YAML frontmatter (Claude Code skill format).

**Deviation from spec, flagged:** The spec calls `sb release` "the only Python/TDD-able unit in A" and frames the loop ledger as inline skill bookkeeping. This plan adds a tested `sb/loopledger.py` helper (invoked as `python -m sb.loopledger`, **not** a new `sb` engine verb) because the diagnostic's productive-vs-churn classification (§8) is real logic that must be both token-free and correct — inline bash cannot be unit-tested and model reasoning is not token-free. The engine's `sb` verb surface gains exactly one verb (`release`), matching the spec.

---

## Background the implementer needs

You are extending an existing, fully-tested engine (Plans 1+2, 122 tests green via `.venv/bin/pytest -q`). **Do not reinvent its primitives — import them.** Relevant interfaces, all on disk and green:

- `sb.paths` — `Layout(repo)` with attrs `.repo .root .tasks .leases .heartbeats .results .config_path .decisions .plans`, method `.lane(name)`; `LANES = ["queued","active","paused","done","failed"]`; `init(repo) -> Layout`; `load_config(lay) -> dict` (merges `DEFAULT_CONFIG`: `verifier_tier="sonnet", verifier_tier_fallback="opus", max_attempts=3, lease_ttl_s=5400, max_chain_depth=3`).
- `sb.store` — `read_json(path)`, `write_json(path, obj)` (atomic via `os.replace`), `fname(task_id)` (`/`→`_`, `+".json"`), `task_path(lay, lane, id)`, `write_task(lay, lane, task)` (validates), `list_tasks(lay, lane) -> [task]` (sorted), `move_task(lay, src, dst, id) -> bool` (atomic rename; `False` = lost race), `find_task(lay, id) -> (lane|None, task|None)`, `done_ids(lay) -> set`.
- `sb.claims` — `claim_one(lay, worker_id, tier=None, cfg=None)`, `claim_wait(lay, worker_id, tier=None, cfg=None, wait_s=0, poll_s=0.5)`, `deps_met(task, completed_set)`, `requeue_stale(lay, cfg)`, `heartbeat(lay, worker_id)`. **You add `release` here in Task 1.**
- `sb.leases` — `write_lease(lay, id, worker_id, ttl_s)`, `read_lease(lay, id) -> dict|None`, `is_expired(lease, now=None)`, `clear_lease(lay, id)`.
- `sb.results.file_result(lay, cfg, task_id) -> dest_lane` — the only door results enter through. A `success` result → task to `paused` (status `awaiting_verification`) and a verify task enqueued in `queued`; `blocked` → `paused` (status `paused_for_human`); other outcomes increment `attempts` and requeue or fail. A verify result (task with `context.verifies` set) with `verdict:pass` promotes the target to `done`; `verdict:fail` reopens it.
- `sb.validate.check(name, obj)` — raises `ValueError` on schema failure. `NAMES` maps `task|plan|decision|result|digest`.

**The infra-vs-task-failure split (spec §5, §8):** `claims.requeue_stale` already requeues a stale-lease task with **attempts unchanged** (infra failure). `results.file_result`'s requeue path **increments attempts** (task failure). There is no path today to deliberately requeue a *named, still-active* task attempts-unchanged — that is exactly what `sb release` adds, for the loop's reactive rate-limit handling on dispatch.

**Lane/claim mechanics that matter for the loop and tests:**
- `claims.claimable` scans **only the `queued` lane** and filters by `deps_met`. GATE tasks live in `paused` with `status="paused_for_human"`, so the loop never claims them. Good — leave it that way.
- The write-before-move invariant: a task body is finalized **before** the atomic rename into a claimable lane; the lease is cleared **before** the rename so a fresh claimer's lease is never clobbered (see `claims.requeue_stale` and `results.file_result` for the canonical pattern — mirror it exactly in `release`).
- `seed.seed` sets `context.branch = f"sb/{plan_id}/{phase_id}".lower()` (e.g. `sb/plan-001/ph-1`) on every task. **`context.branch` is the authoritative branch field** — the loop reads it, not a recomputed `<plan>/<phase>` string (the spec's `branch = "<plan_id>/<phase_id>".lower()` in §2 is shorthand; the real field carries the `sb/` prefix). Verify tasks copy `branch` from their author (`results._enqueue_verification`).

**tiers.json** (repo root, tracked, unchanged in role) maps abstract tier → model id:
```json
{ "tiers": { "fable": "claude-fable-5", "opus": "claude-opus-4-8",
             "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5" } }
```
The skill reads this to pick the `model` override for the dispatched subagent.

**AgDR shape** the task protocol must reference (decision schema v0.3.0, see `tests/helpers.make_agdr` for a complete valid example): records carry `steelman` (array of `{option, strongest_case}`) and `blast_radius` (free string) — the ADR-043 template fields (PHI-028). Phase AgDRs carry `provenance: {plan_id, phase_id, task_id}`. Status `pending-review` is the HDR-010 tier-2 state.

**Skill location:** `.claude/skills/sb-work/` — tracked in git (`.gitignore` excludes only `.claude/settings.local.json` and `.claude/scheduled_tasks.lock`), repo-canonical (not user-level — avoids the skill-drift divergence trap, spec §1). Claude Code discovers project skills here.

---

## File structure

```
sb/claims.py                              # MODIFY: add release(lay, task_id) — infra-requeue a named active task
sb/cli.py                                 # MODIFY: add `release` subcommand
sb/loopledger.py                          # NEW: token-free iteration ledger append + productive/churn diagnostic
tests/test_release.py                     # NEW: TDD for release (lane move + attempts-unchanged)
tests/test_loopledger.py                  # NEW: TDD for append + diagnose
tests/test_worker_loop_integration.py     # NEW: stub-dispatcher choreography + worktree lifecycle (real git, no model)
.claude/skills/sb-work/SKILL.md           # NEW: the /sb-work worker loop
.claude/skills/sb-work/task-protocol.md   # NEW: task subagent dispatch prompt protocol
.claude/skills/sb-work/verifier-protocol.md # NEW: verifier subagent dispatch prompt protocol
```

Convention notes:
- `release` is a sibling of `requeue_stale` — same module (`claims.py`), same write-before-move + clear-lease-before-rename discipline, same attempts-unchanged contract. It differs only in targeting one named active task (not a stale sweep) and not checking the lease.
- `sb/loopledger.py` is **not** imported by any engine module. It is a leaf helper the skill shells out to (`python -m sb.loopledger ...`). Keeping it off the engine import graph keeps the engine's "git-free, deterministic core" boundary clean.
- The integration test owns a tiny throwaway git repo (created in the test) because it exercises `git worktree add/remove`. It stubs the *model dispatch* (writes a canned result file) but uses the **real** `sb` engine functions and **real** git — that is the point of §6's "stub dispatcher."
- The three skill `.md` files are prose. They are reviewed against the spec (Task 4–6 self-review step) and exercised live in the exit bar (D). They have no pytest coverage by design — this is the milestone's lowest-confidence surface and the spec names it as such (§6).

---

### Task 1: `sb release` — infra-requeue a named active task

**Files:**
- Modify: `sb/claims.py` (add `release` after `requeue_stale`)
- Modify: `sb/cli.py` (add `release` subparser + handler)
- Test: `tests/test_release.py`

- [ ] **Step 1: Write the failing test**

`tests/test_release.py`:
```python
import os

import pytest

from sb import claims, leases, store
from sb.paths import LANES
from tests.helpers import make_task


def seed(lay, *tasks, lane="queued"):
    for t in tasks:
        store.write_task(lay, lane, t)


def test_release_requeues_active_task_attempts_unchanged(lay):
    seed(lay, make_task("PLAN-001/PH-1/T-1"))
    got = claims.claim_one(lay, "w1")
    assert got["attempts"] == 0
    assert store.find_task(lay, got["id"])[0] == "active"

    dest = claims.release(lay, got["id"])

    assert dest == "queued"
    lane, t = store.find_task(lay, got["id"])
    assert lane == "queued"
    assert t["status"] == "queued"
    assert t["attempts"] == 0          # infra requeue: attempts UNCHANGED
    assert "claim" not in t            # claim dropped
    assert leases.read_lease(lay, got["id"]) is None  # lease dropped


def test_release_leaves_exactly_one_file(lay):
    seed(lay, make_task())
    got = claims.claim_one(lay, "w1")
    claims.release(lay, got["id"])
    hits = [lane for lane in LANES
            if os.path.exists(store.task_path(lay, lane, got["id"]))]
    assert hits == ["queued"]


def test_release_rejects_non_active(lay):
    seed(lay, make_task())  # task is in queued, never claimed
    with pytest.raises(ValueError, match="not active"):
        claims.release(lay, "PLAN-001/PH-1/T-1")


def test_release_rejects_unknown(lay):
    with pytest.raises(ValueError, match="not active"):
        claims.release(lay, "PLAN-001/PH-1/NOPE")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_release.py -v`
Expected: FAIL with `AttributeError: module 'sb.claims' has no attribute 'release'`

- [ ] **Step 3: Write minimal implementation**

In `sb/claims.py`, add after `requeue_stale` (mirrors its write-before-move + clear-lease-before-rename discipline, but targets one named active task and keeps attempts unchanged):
```python
def release(lay, task_id):
    """Infra-requeue a named active task: active -> queued, attempts UNCHANGED,
    lease dropped. The loop calls this when a dispatch raises a rate-limit /
    usage-cap signal (infra failure, not task failure — spec §5/§8). Unlike
    file-result's requeue path it never increments attempts; unlike
    requeue_stale it targets one task and does not consult the lease.

    Same ordering as requeue_stale: finalize the body while still in active/
    (un-claimable), clear the lease before the rename so a fresh claimer's lease
    is never clobbered, then the atomic move. Never write after the move."""
    lane, task = store.find_task(lay, task_id)
    if lane != "active":
        where = f"lane={lane}" if lane else "not found in any lane"
        raise ValueError(f"{task_id} is not active ({where}); cannot release")
    task["status"] = "queued"
    task.pop("claim", None)
    store.write_task(lay, "active", task)
    leases.clear_lease(lay, task_id)
    if not store.move_task(lay, "active", "queued", task_id):
        raise ValueError(f"{task_id} vanished from active while releasing")
    return "queued"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_release.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Wire the CLI**

In `sb/cli.py`, add the subparser near the other `common(...)` parsers (after the `file-result` block):
```python
    p = common(sub.add_parser("release"))
    p.add_argument("task_id")
```
And add the handler after the `file-result` handler block:
```python
    if a.cmd == "release":
        dest = claims.release(lay, a.task_id)
        _out({"task_id": a.task_id, "lane": dest})
        return 0
```

- [ ] **Step 6: Add a CLI smoke test**

Append to `tests/test_release.py`:
```python
def test_cli_release(lay, capsys):
    import json

    from sb import cli
    seed(lay, make_task())
    claims.claim_one(lay, "w1")
    rc = cli.main(["release", "PLAN-001/PH-1/T-1", "--repo", lay.repo])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"task_id": "PLAN-001/PH-1/T-1", "lane": "queued"}
    assert store.find_task(lay, "PLAN-001/PH-1/T-1")[0] == "queued"
```

- [ ] **Step 7: Run the full release test file**

Run: `.venv/bin/pytest tests/test_release.py -v`
Expected: PASS (5 passed)

- [ ] **Step 8: Commit**

```bash
git add sb/claims.py sb/cli.py tests/test_release.py
git commit -m "feat(sb): add release verb — infra-requeue a named active task, attempts unchanged"
```

---

### Task 2: Loop ledger + diagnostic helper (`sb/loopledger.py`)

**Files:**
- Create: `sb/loopledger.py`
- Test: `tests/test_loopledger.py`

This is the token-free instrumentation behind the loop-cap **diagnostic checkpoint** (spec §8). `append` writes one JSONL line per loop iteration; `diagnose` aggregates the ledger into the productive-vs-churn split written at the checkpoint. Pure functions over disk state, no model reasoning.

Ledger line shape (one per iteration): `{"i": int, "claimed_id": str|null, "type": str, "outcome": str, "released": bool, "wall_s": float}`.
- `claimed_id` is null on an idle pass (claim timed out, nothing claimed).
- `type` is `"task"` or `"verify"` (does the claimed task carry `context.verifies`?).
- `outcome` is the lane `file-result` returned (`"paused"`, `"done"`, `"queued"`, `"failed"`), or `"released"` for a rate-limit release, or `"idle"` for an empty claim.

Diagnostic semantics (testable definitions):
- `total_iterations` = number of ledger lines.
- `distinct_tasks` = count of distinct non-null `claimed_id`.
- `productive` = count of lines with `outcome == "done"` (a verify pass that promoted a target to done — "distinct tasks reaching done", §8).
- `retries` = count of lines whose `claimed_id` (non-null) appeared on an earlier line (repeated claim = the task came back).
- `releases` = count of lines with `released` truthy (the quota-event count, §8).
- `churn` = `releases + retries`.
- `wall_s_total` = sum of `wall_s`.

- [ ] **Step 1: Write the failing test**

`tests/test_loopledger.py`:
```python
import json
import os

from sb import loopledger


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_append_writes_one_jsonl_line_per_call(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id="P/PH/T-1", type="task",
                      outcome="paused", released=False, wall_s=12.5)
    loopledger.append(led, i=1, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=8.0)
    lines = read_lines(led)
    assert len(lines) == 2
    assert lines[0] == {"i": 0, "claimed_id": "P/PH/T-1", "type": "task",
                        "outcome": "paused", "released": False, "wall_s": 12.5}
    assert lines[1]["outcome"] == "done"


def test_append_handles_idle_pass(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id=None, type="idle",
                      outcome="idle", released=False, wall_s=30.0)
    assert read_lines(led)[0]["claimed_id"] is None


def test_diagnose_classifies_productive_and_churn(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    # T-1 succeeds (paused→awaiting verify); its verify passes (done).
    loopledger.append(led, i=0, claimed_id="P/PH/T-1", type="task",
                      outcome="paused", released=False, wall_s=10.0)
    loopledger.append(led, i=1, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=5.0)
    # T-2 hits a rate limit and is released (infra), then re-claimed and done.
    loopledger.append(led, i=2, claimed_id="P/PH/T-2", type="task",
                      outcome="released", released=True, wall_s=1.0)
    loopledger.append(led, i=3, claimed_id="P/PH/T-2", type="task",
                      outcome="paused", released=False, wall_s=9.0)
    loopledger.append(led, i=4, claimed_id="P/PH/T-2.V1", type="verify",
                      outcome="done", released=False, wall_s=4.0)
    # one idle pass
    loopledger.append(led, i=5, claimed_id=None, type="idle",
                      outcome="idle", released=False, wall_s=20.0)

    d = loopledger.diagnose(led, worker_id="w1")
    assert d["worker_id"] == "w1"
    assert d["total_iterations"] == 6
    assert d["distinct_tasks"] == 4          # T-1, T-1.V1, T-2, T-2.V1
    assert d["productive"] == 2              # two outcome==done lines
    assert d["releases"] == 1
    assert d["retries"] == 1                 # second P/PH/T-2 is a repeat claim
    assert d["churn"] == 2                   # releases + retries
    assert d["wall_s_total"] == 49.0


def test_diagnose_empty_ledger(tmp_path):
    led = str(tmp_path / "missing.jsonl")
    d = loopledger.diagnose(led, worker_id="w1")
    assert d["total_iterations"] == 0
    assert d["distinct_tasks"] == 0
    assert d["productive"] == 0
    assert d["churn"] == 0
    assert d["wall_s_total"] == 0


def test_diagnose_writes_out_file(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id="P/PH/T-1", type="verify",
                      outcome="done", released=False, wall_s=3.0)
    out = str(tmp_path / "loop-diagnostic-w1.json")
    loopledger.diagnose(led, worker_id="w1", out=out)
    assert json.load(open(out))["productive"] == 1


def test_cli_append_then_diagnose(tmp_path, capsys):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.main(["append", "--ledger", led, "--i", "0",
                     "--claimed-id", "P/PH/T-1.V1", "--type", "verify",
                     "--outcome", "done", "--wall-s", "3.0"])
    rc = loopledger.main(["diagnose", "--ledger", led, "--worker-id", "w1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_iterations"] == 1
    assert out["productive"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_loopledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sb.loopledger'`

- [ ] **Step 3: Write the implementation**

`sb/loopledger.py`:
```python
"""Token-free loop instrumentation for the /sb-work worker loop (spec §8).

The skill shells out to this (`python -m sb.loopledger ...`) once per iteration
to append to a per-worker JSONL ledger, and once at the loop-cap checkpoint to
compute the productive-vs-churn diagnostic. Pure functions over disk state — no
model reasoning, so the bookkeeping costs no tokens and survives session death.

Deliberately off the engine import graph: this is skill-support instrumentation,
not part of the deterministic git-free engine core.
"""

import argparse
import json
import os
import sys


def append(ledger_path, *, i, claimed_id, type, outcome, released, wall_s):
    line = {"i": i, "claimed_id": claimed_id, "type": type,
            "outcome": outcome, "released": bool(released), "wall_s": wall_s}
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _read(ledger_path):
    if not os.path.exists(ledger_path):
        return []
    out = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def diagnose(ledger_path, *, worker_id, out=None):
    lines = _read(ledger_path)
    seen = set()
    distinct = set()
    productive = retries = releases = 0
    wall_total = 0.0
    for ln in lines:
        cid = ln.get("claimed_id")
        if cid is not None:
            if cid in seen:
                retries += 1
            seen.add(cid)
            distinct.add(cid)
        if ln.get("outcome") == "done":
            productive += 1
        if ln.get("released"):
            releases += 1
        wall_total += ln.get("wall_s", 0) or 0
    diag = {
        "worker_id": worker_id,
        "total_iterations": len(lines),
        "distinct_tasks": len(distinct),
        "productive": productive,
        "retries": retries,
        "releases": releases,
        "churn": releases + retries,
        "wall_s_total": wall_total,
    }
    if out:
        tmp = f"{out}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(diag, f, indent=2)
        os.replace(tmp, out)
    return diag


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sb.loopledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append")
    a.add_argument("--ledger", required=True)
    a.add_argument("--i", type=int, required=True)
    a.add_argument("--claimed-id", default=None)
    a.add_argument("--type", required=True)
    a.add_argument("--outcome", required=True)
    a.add_argument("--released", action="store_true")
    a.add_argument("--wall-s", type=float, default=0.0)

    d = sub.add_parser("diagnose")
    d.add_argument("--ledger", required=True)
    d.add_argument("--worker-id", required=True)
    d.add_argument("--out", default=None)

    ns = ap.parse_args(argv)
    if ns.cmd == "append":
        append(ns.ledger, i=ns.i, claimed_id=ns.claimed_id, type=ns.type,
               outcome=ns.outcome, released=ns.released, wall_s=ns.wall_s)
        return 0
    if ns.cmd == "diagnose":
        diag = diagnose(ns.ledger, worker_id=ns.worker_id, out=ns.out)
        print(json.dumps(diag, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_loopledger.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add sb/loopledger.py tests/test_loopledger.py
git commit -m "feat(sb): token-free loop ledger + productive/churn diagnostic (spec §8)"
```

---

### Task 3: Worker-loop integration test (stub dispatcher)

**Files:**
- Test: `tests/test_worker_loop_integration.py`

This is the §6 "stub dispatcher" integration test: exercise the loop's choreography (claim → [stub writes result] → file-result → lane move) plus the **worktree create/remove** lifecycle, deterministically, with **real `sb` engine + real git** and **no model call**. It is a regression guard on the contract the skill depends on. There is no new production code to make it pass — it characterizes existing engine behavior plus `git worktree`. If it fails, that is a real finding about the engine or the worktree assumptions, not a missing implementation.

- [ ] **Step 1: Write the test**

`tests/test_worker_loop_integration.py`:
```python
"""Stub-dispatcher integration test for the /sb-work loop (spec §6).

Stands in for the model: a 'dispatch' just writes a canned result file. Uses the
real sb engine and real `git worktree` to assert the choreography the skill
relies on — claim, worktree create, file-result lane move, worktree remove,
then the verify pass promoting the target to done.
"""
import json
import os
import subprocess

import pytest

from sb import claims, paths, results, store
from tests.helpers import make_task


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path)
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("seed\n")
    git(r, "add", "README.md")
    git(r, "commit", "-qm", "init")
    return r


def write_result(lay, task_id, **over):
    res = {"schema_version": "0.1.0", "outcome": "success",
           "summary": "did the thing"}
    res.update(over)
    store.write_json(os.path.join(lay.results, store.fname(task_id)), res)


def test_loop_choreography_and_worktree_lifecycle(repo):
    lay = paths.init(repo)
    cfg = paths.load_config(lay)
    task = make_task("PLAN-001/PH-1/T-1", tier="haiku",
                     context={"branch": "sb/plan-001/ph-1", "depends_on": []},
                     done={"statement": "thing done",
                           "verify": {"kind": "command", "ref": "true"}})
    store.write_task(lay, "queued", task)

    # 1. claim
    claimed = claims.claim_one(lay, "w1", cfg=cfg)
    assert claimed["id"] == "PLAN-001/PH-1/T-1"
    assert store.find_task(lay, claimed["id"])[0] == "active"

    # 2. provision worktree off the phase branch (skill's job; done here w/ real git)
    branch = claimed["context"]["branch"]
    wt = os.path.join(repo, ".worktrees", "w1")
    git(repo, "worktree", "add", "-q", "-b", branch, wt, "HEAD")
    assert os.path.isdir(wt)

    # 3. stub dispatch: subagent would commit work + write the result file
    (open(os.path.join(wt, "f.txt"), "w")).write("work\n")
    git(wt, "add", "f.txt")
    git(wt, "commit", "-qm", "task work")
    write_result(lay, claimed["id"])

    # 4. file-result: success -> paused (awaiting verification) + verify enqueued
    dest = results.file_result(lay, cfg, claimed["id"])
    assert dest == "paused"
    lane, t = store.find_task(lay, claimed["id"])
    assert lane == "paused" and t["status"] == "awaiting_verification"
    verify_id = "PLAN-001/PH-1/T-1.V1"
    vlane, vtask = store.find_task(lay, verify_id)
    assert vlane == "queued" and vtask["context"]["verifies"] == claimed["id"]

    # 5. teardown: branch + commit persist after worktree removal
    git(repo, "worktree", "remove", "--force", wt)
    assert not os.path.isdir(wt)
    log = subprocess.run(["git", "log", "--oneline", branch], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    assert "task work" in log

    # 6. verify pass promotes the target to done
    vclaimed = claims.claim_one(lay, "w1", cfg=cfg)
    assert vclaimed["id"] == verify_id
    write_result(lay, verify_id, verdict="pass", verdict_notes="looks right")
    vdest = results.file_result(lay, cfg, verify_id)
    assert vdest == "done"
    assert store.find_task(lay, claimed["id"]) == ("done", store.find_task(
        lay, claimed["id"])[1])
    assert store.find_task(lay, claimed["id"])[1]["status"] == "done"


def test_loop_releases_on_simulated_rate_limit(repo):
    """A dispatch that 'raises a rate-limit' before producing a result -> the
    loop calls release; the task is claimable again with attempts unchanged."""
    lay = paths.init(repo)
    cfg = paths.load_config(lay)
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    claimed = claims.claim_one(lay, "w1", cfg=cfg)
    # simulate: dispatch raised before writing any result -> release
    dest = claims.release(lay, claimed["id"])
    assert dest == "queued"
    again = claims.claim_one(lay, "w2", cfg=cfg)
    assert again["id"] == "PLAN-001/PH-1/T-1"
    assert again["attempts"] == 0
```

- [ ] **Step 2: Run the test**

Run: `.venv/bin/pytest tests/test_worker_loop_integration.py -v`
Expected: PASS (2 passed). If either fails, stop and investigate — it is a real finding about the engine contract or worktree assumptions the skill will rely on.

- [ ] **Step 3: Run the full suite to confirm no regression**

Run: `.venv/bin/pytest -q`
Expected: PASS (all prior tests + the new ones green)

- [ ] **Step 4: Commit**

```bash
git add tests/test_worker_loop_integration.py
git commit -m "test(sb): stub-dispatcher integration test — loop choreography + worktree lifecycle (spec §6)"
```

---

### Task 4: `/sb-work` skill — `SKILL.md`

**Files:**
- Create: `.claude/skills/sb-work/SKILL.md`

Prose deliverable — no test. The full worker loop from spec §2, with the worktree lifecycle (§4), the infra-vs-task failure handling (§5), and the loop-cap diagnostic checkpoint (§8). Reviewed against the spec in Step 2.

- [ ] **Step 1: Write the skill**

`.claude/skills/sb-work/SKILL.md`:
````markdown
---
name: sb-work
description: Run a Switchboard worker session — a long-running interactive loop that claims tasks from the sb file-queue, dispatches each to a fresh-context subagent in an isolated git worktree at the task's tier, files the validated result, and tears the worktree down. Use to start a worker (one per terminal); the loop self-paces and is killable without data loss.
---

# sb-work — the worker loop

You are a **Switchboard worker session**. You never do task work in your own
context. Each loop pass claims one task, provisions an isolated git worktree,
dispatches a **fresh-context subagent** to do the work at the task's tier, files
the result through the engine, and tears the worktree down. Your context grows
only by loop bookkeeping (~hundreds of tokens/task) — so this session can run
for a very long time and is **disposable**: kill it anytime, start a fresh one,
nothing is lost (all state is on disk).

The `sb` engine is git-free and deterministic. **You own all git operations.**
The engine owns lane state, leases, validation, and result routing. Never parse
a subagent's freeform output — the result *file* is the only channel.

## Setup (once per session)

1. Pick a stable `WORKER_ID` for this session, e.g. `<hostname>-<pid>` or a name
   the operator gave you. Use it for every `sb` call and the ledger filename.
2. Set `W`, the claim wait window in seconds (start at `300`; lengthen on quota
   pressure — see Backoff).
3. Set `MAX_LOOP_ITERATIONS` from the session flag/config (default **200**).
   This is a **diagnostic checkpoint, not a kill** (see Loop checkpoint).
4. Note the **integration base** — the branch this session is on at start
   (e.g. `design/switchboard-v2`). New phase branches are cut from it.
5. `LEDGER=.switchboard/loop-ledger-$WORKER_ID.jsonl`. Initialize `i=0`.

## The loop

Repeat until the operator stops the session:

1. **Claim** (blocks in-tool; costs no tokens while waiting):
   ```
   sb claim --wait $W --worker-id $WORKER_ID
   ```
   - **exit 3** (nothing to claim): run `sb heartbeat --worker-id $WORKER_ID`,
     then record an idle ledger line and continue:
     ```
     python -m sb.loopledger append --ledger $LEDGER --i $i \
       --type idle --outcome idle --wall-s <elapsed>
     ```
     Increment `i`, check the loop checkpoint, repeat. (If quota is throttled,
     lengthen `W` first — see Backoff.)
   - **exit 0**: the task JSON is on stdout. Continue with it as `T`.
2. **Heartbeat**: `sb heartbeat --worker-id $WORKER_ID` (feeds the stale-fleet
   signal, spec §7).
3. **Resolve the branch**: `BRANCH = T.context.branch` (authoritative — e.g.
   `sb/plan-001/ph-1`). If absent, fall back to
   `"sb/<plan_id>/<phase_id>".lower()` from `T.source`.
4. **Ensure the branch exists**, then provision the worktree (isolation BEFORE
   any task code runs — PHI-033):
   ```
   git show-ref --verify --quiet refs/heads/$BRANCH \
     || git branch $BRANCH <integration-base>
   WT=.worktrees/$WORKER_ID
   git worktree add "$WT" $BRANCH
   ```
   (If `$WT` already exists from a prior crashed pass, `git worktree remove
   --force "$WT"` first.)
5. **Pick the model**: read `tiers.json` (repo root); `MODEL = tiers[T.tier]`.
   Tier lives on the dispatch, not the session — any worker serves any tier.
6. **Dispatch a fresh-context subagent** with the `model` override, working in
   `$WT`:
   - If `T.context.verifies` is set → use **verifier-protocol.md**.
   - Otherwise → use **task-protocol.md**.
   Fill the protocol template from `T` (goal, `done`, constraints, grounding via
   `sb query`, prior result if this is a retry/continuation, the worktree CWD).
   The subagent commits its work to `$BRANCH` and writes
   `.switchboard/results/<T.id>.json` against the result schema. It is the only
   thing that writes that file; you do not.
   - **If the dispatch raises a rate-limit / usage-cap signal** (infra failure,
     not task failure): `sb release <T.id>` (→ queued, attempts unchanged),
     apply Backoff, remove the worktree, record a ledger line with
     `--outcome released --released`, and continue. Do **not** call file-result.
7. **File the result** (the engine validates, moves the lane, enqueues the
   verification task on success):
   ```
   sb file-result <T.id>
   ```
   Capture the returned `lane` as the iteration `OUTCOME`.
8. **Tear down the worktree** (commits persist on the branch; the result lives
   in `.switchboard/`, not the worktree, so teardown is safe even if dirty):
   ```
   git worktree remove --force "$WT"
   ```
9. **Record the iteration** and advance:
   ```
   python -m sb.loopledger append --ledger $LEDGER --i $i \
     --claimed-id <T.id> \
     --type $( [ -n "$T.context.verifies" ] && echo verify || echo task ) \
     --outcome $OUTCOME --wall-s <elapsed>
   ```
   `i = i + 1`.
10. **Loop checkpoint** (see below). Then repeat from step 1.

## Backoff (quota is advisory, never a claim gate — HDR-011)

`.switchboard/quota.json` (written by sub-plan B's token-free detector; may be
absent in M0) is **advisory only**. If present and it advises a throttled/
exhausted state, **lengthen `W`** (e.g. double it, cap ~1800s) so claims wait
longer between attempts. Never refuse to claim because of quota — a claim is
always allowed; only the wait window changes. On a clean stretch, relax `W` back
toward the 300s default.

## Loop checkpoint (diagnostic, NOT a kill — spec §8)

When `i >= MAX_LOOP_ITERATIONS`:

1. Compute and persist the diagnostic:
   ```
   python -m sb.loopledger diagnose --ledger $LEDGER --worker-id $WORKER_ID \
     --out .switchboard/loop-diagnostic-$WORKER_ID.json
   ```
   It reports total iterations, `distinct_tasks`, `productive` (tasks reaching
   `done`) vs `churn` (`releases + retries`), and total wallclock.
2. Fire a notification so the operator reviews it:
   `sb notify` (the digest/notify layer surfaces it).
3. **PAUSE claiming** — do **not** exit or kill the session. Tell the operator:
   *"Worker $WORKER_ID hit the loop checkpoint at $i iterations — diagnostic
   written to .switchboard/loop-diagnostic-$WORKER_ID.json. Review and resume."*
   Wait for the operator.
4. On **resume**: reset `i = 0` (optionally raise `MAX_LOOP_ITERATIONS` if the
   operator says so) and continue the loop.

Why a checkpoint, not a drift-kill: a raw iteration count conflates healthy high
throughput with unproductive looping. The diagnostic separates the two; at a
high default the common case is "all productive → bump and continue." The sharp
early churn detector (consecutive no-progress) is sub-plan B's tripwire — this
skill owns only the coarse periodic checkpoint.

## Invariants you must not break

- The result file is the only channel from a subagent. Never act on freeform
  subagent output.
- Worktree provisioned **before** dispatch, removed **after** filing — isolation
  is guaranteed, not hoped for.
- A rate-limit on dispatch → `sb release` (attempts unchanged). A verifier
  rejection or attempt exhaustion is the only path to `failed` — and that is the
  engine's job inside `file-result`, never yours.
- The engine does no git; you do no lane-state mutation except through `sb`
  verbs (`claim`, `file-result`, `release`, `heartbeat`).
````

- [ ] **Step 2: Self-review against the spec**

Re-read `docs/specs/2026-06-16-sb-worker-loop-design.md` §2, §4, §5, §8 with the skill open. Confirm each loop line in §2 maps to a step here; each property (stateless, heartbeat per pass, fresh context, any worker any tier) is stated; the rate-limit→release path and the checkpoint semantics match §5 and §8. Fix any drift inline.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/SKILL.md
git commit -m "feat(sb): /sb-work worker loop skill (spec §2/§4/§5/§8)"
```

---

### Task 5: Task subagent protocol — `task-protocol.md`

**Files:**
- Create: `.claude/skills/sb-work/task-protocol.md`

The dispatch prompt template for a normal (non-verification) task. Prose — no test. Carries everything spec §3 (task protocol) requires: goal, `done.statement` + `done.verify`, constraints, grounding, the prior result on a retry/continuation, the AgDR-instead-of-prompt protocol, hard-escalation blockers, and the worktree CWD.

- [ ] **Step 1: Write the protocol**

`.claude/skills/sb-work/task-protocol.md`:
````markdown
# Task subagent protocol

The worker fills this template and dispatches it to a **fresh-context** subagent
with the model override for `T.tier`. The subagent does the work in the provided
worktree, commits to the phase branch, and writes a single result file. It never
returns work through chat — only through the result file.

## Dispatch prompt template

> You are executing one Switchboard task in a fresh context. Do the work, commit
> it, and write a result file. Do not ask for input — see "When you would ask a
> human" below.
>
> **Working directory (CWD):** `{worktree_path}` — a git worktree of branch
> `{branch}`. All file work and commits happen here.
>
> **Goal:** {T.goal}
>
> **Definition of done:**
> - Statement: {T.done.statement}
> - Machine check: {T.done.verify.kind} → `{T.done.verify.ref}`
>   {if expect}(expect: {T.done.verify.expect}){endif}
>   Run it yourself before filing; your result must reflect its real outcome.
>
> **Constraints (hard — stop and file `blocked` rather than violate one):**
> {bullet list of T.context.constraints, or "none"}
>
> **Grounding (read before starting — precedent, not first principles):**
> {for each id in T.context.grounding: the digest from `sb query`}
>
> **Prior attempt(s)** {only if T.context.prior_attempts is non-empty}:
> The earlier attempt(s) below did not pass. Read them and do not repeat the
> same approach blindly:
> {summaries + verifier_notes of each prior attempt}
>
> ## When you would ask a human (AgDR-instead-of-prompt — PHI-028)
> At any decision point where you would normally stop and ask: instead research
> it (inline, within your depth), then **write an AgDR** to `decisions/ADR-NNN.json`
> using the ADR-043 template and proceed on your best judgment. The AgDR MUST
> include:
> - `steelman`: the strongest case for each rejected option (`[{option,
>   strongest_case}]`).
> - `blast_radius`: a plain-language note on what this decision affects if wrong.
> - `provenance`: `{plan_id, phase_id, task_id}` copied from this task's `source`.
> - `status: "pending-review"`, `confidence: high|medium|low`.
> List the AgDR id in your result's `decisions_emitted`.
>
> ## Hard-escalation domains — these are TRUE blockers, do NOT proceed
> If the task requires crossing a security boundary, a production deploy,
> handling secrets, or changing a frozen contract: do **not** proceed and do
> **not** write an AgDR to override it. File a `blocked` result with a clear
> `summary` of what is blocked and why, and stop.
>
> ## Finishing
> 1. Commit your work to branch `{branch}` (clear messages; small commits ok).
> 2. Write `.switchboard/results/{T.id}.json` validating against the result
>    schema:
>    - `schema_version: "0.1.0"`
>    - `outcome`: `success` (done + machine check passed) | `partial` |
>      `blocked` (hard-escalation or genuinely cannot proceed) | `failed`.
>    - `summary`: 2–3 sentences, a handoff digest, never a transcript.
>    - `evidence`: e.g. `[{kind:"commit", ref:"<sha>"}, {kind:"test",
>      ref:"<cmd>", result:"pass"}]`.
>    - `decisions_emitted`: any AgDR ids you wrote.
> 3. Do not move the task between lanes and do not run any `sb` command — the
>    worker files your result.

## Notes for the worker filling this template

- Only inline the fields that exist on `T`; omit empty sections (don't emit an
  empty "Prior attempt(s)" or "Constraints" block).
- Resolve `grounding` ids through `sb query` so the subagent gets digests, not
  raw record dumps.
- The CWD line is mandatory — it is the isolation guarantee (PHI-033).
````

- [ ] **Step 2: Self-review against the spec**

Re-read spec §3 (Task protocol) and §5 (AgDR / hard-escalation). Confirm the template carries goal, `done.statement`+`done.verify`, constraints, grounding, prior result on retry, the ADR-043 AgDR fields (steelman + blast-radius), the hard-escalation blocker list, and the worktree CWD; and that the result file is the only output channel. Fix drift inline.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/task-protocol.md
git commit -m "feat(sb): task subagent dispatch protocol (spec §3/§5)"
```

---

### Task 6: Verifier subagent protocol — `verifier-protocol.md`

**Files:**
- Create: `.claude/skills/sb-work/verifier-protocol.md`

The dispatch prompt template for a verification task (one whose `context.verifies`
is set). Prose — no test. Per spec §3 (verifier protocol) and §6: a different
model than the author, fresh context, runs the machine check and judges the
committed diff against `done.statement`, and writes a `verdict: pass|fail`.

- [ ] **Step 1: Write the protocol**

`.claude/skills/sb-work/verifier-protocol.md`:
````markdown
# Verifier subagent protocol

The worker fills this template for a verification task (`T.context.verifies` is
set) and dispatches it to a **fresh-context** subagent. The engine already routed
this task to a tier **different from the author's** (`verifier_tier`, with a
fallback when it would collide) — so verification is independent by construction
(PHI-030). Only a verifier `pass` moves the target to `done`; a `fail` reopens it
with this verdict carried into the retry prompt. The engine enforces this inside
`file-result`; the subagent only judges and reports.

## Dispatch prompt template

> You are an **independent verifier** in a fresh context. You did not write this
> work. Judge it honestly — a false `pass` is worse than a `fail`.
>
> **Working directory (CWD):** `{worktree_path}` — a git worktree of branch
> `{branch}` containing the author's committed work.
>
> **Task under verification:** `{T.context.verifies}`
> **Original goal:** {target.goal}
> **Definition of done:**
> - Statement: {target.done.statement}
> - Machine check: {target.done.verify.kind} → `{target.done.verify.ref}`
>   {if expect}(expect: {target.done.verify.expect}){endif}
>
> ## What to do
> 1. **Run the machine check** (`{target.done.verify.ref}`) yourself in the
>    worktree and record its real result. If there is no machine check, inspect
>    the committed diff directly.
> 2. **Judge the committed diff against the done statement** — not just whether
>    the command exits 0, but whether the work actually satisfies the stated
>    outcome (no faked tests, no scope gaps, no obvious correctness holes).
> 3. **Write the result file** `.switchboard/results/{T.id}.json`:
>    - `schema_version: "0.1.0"`
>    - `outcome: "success"` (you completed the verification — this is about the
>      verification running, not the verdict).
>    - `verdict`: `"pass"` (work satisfies the done statement and the check
>      passed) or `"fail"`.
>    - `verdict_notes`: concrete reasons — what you ran, what you saw, and for a
>      `fail`, exactly what is missing or wrong (this text is carried into the
>      author's retry prompt, so make it actionable).
>    - `evidence`: e.g. `[{kind:"test", ref:"<cmd>", result:"pass|fail"}]`.
> 4. Do not fix the work, do not commit, do not run any `sb` command — the worker
>    files your result and the engine applies the verdict.

## Notes for the worker filling this template

- Resolve `{target.*}` by reading the task being verified (`T.context.verifies`)
  — its `goal` and `done` are what the verifier judges against.
- The verifier works in the **same phase branch worktree** as the author's
  commits so the diff is present to inspect.
- Never let the same model that authored the work verify it — the engine's tier
  routing already prevents this; do not override the dispatched tier.
````

- [ ] **Step 2: Self-review against the spec**

Re-read spec §3 (Verifier protocol) and §6. Confirm: different model / fresh context, runs `done.verify`, judges the diff against `done.statement`, writes `verdict: pass|fail` + `verdict_notes`, and that only the engine applies the verdict. Fix drift inline.

- [ ] **Step 3: Final full-suite run**

Run: `.venv/bin/pytest -q`
Expected: PASS (all green — engine additions + integration test).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/sb-work/verifier-protocol.md
git commit -m "feat(sb): verifier subagent dispatch protocol (spec §3/§6)"
```

---

## Out of scope (deferred deliberately — spec §7)

Do **not** build these here; they are named follow-ons:
- **Planner protocol + `sb seed --goal`** → `A-planner` (before D).
- **Research-handoff / continuation** (`paused_for_research` → `sb spawn` →
  continuation task) → its own follow-on before D. `sb spawn` exists; this needs
  a result-outcome + re-enqueue, real engine work, not A's spine.
- **Tripwire guards + token-free quota detection** (`quota.json` writer, the
  sharp consecutive-no-progress churn detector) → sub-plan B. A only *reads*
  `quota.json` advisorily and owns the coarse periodic checkpoint.

## Self-review checklist (run after writing, before execution)

1. **Spec coverage:** §2 loop → Task 4 SKILL.md; §3 task protocol → Task 5; §3
   verifier protocol → Task 6; §4 worktree lifecycle → Task 4 + Task 3 test; §5
   infra-vs-task / `sb release` → Task 1 + Task 4; §6 testing (release TDD,
   stub-dispatcher integration, protocols reviewed) → Tasks 1/3/4–6; §8 loop cap
   diagnostic → Task 2 + Task 4. §7 scope lines → "Out of scope" above. Covered.
2. **Placeholders:** none — every code/prose step shows full content.
3. **Type/name consistency:** `release(lay, task_id)` returns `"queued"` and is
   called identically in CLI, tests, and SKILL.md; `loopledger.append/diagnose`
   signatures match across test, module, and the skill's `python -m` calls;
   `context.branch` (not a recomputed string) is the branch source everywhere.
````
