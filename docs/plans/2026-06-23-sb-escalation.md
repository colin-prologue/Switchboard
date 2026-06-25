# Plan 3-C Escalation Layer Implementation Plan — tier calibration + `sb resolve`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the independent verifier calibrate each emitted AgDR into HDR-010's three tiers (expressed as three notify cadences — immediate / digest / gate-only), and add `sb resolve` to re-queue a `paused_for_human` task while optionally capturing the human's resolution as queryable grounding (ADR-004).

**Architecture:** Tier is carried on the AgDR's `tags` (`escalation:interrupt` / `escalation:record-silent`; untagged = flag-async, the fail-safe default) — no schema field, no decision-schema bump (C-4). The digest partitions `pending-review` AgDRs by that tag into three buckets; `sb notify` pings `interrupt_agdrs` + `pending_agdrs` (flag-async) but never `record_silent_agdrs`. The verifier writes the tag (a `verifier-protocol.md` section, reviewed-not-tested); the worker fires an immediate `sb notify` on interrupt (a SKILL wire, reviewed-not-tested). `sb resolve` is a new engine verb in `sb/resolve.py`. Interrupt does **not** block the queue — the task still reaches `done`; the phase GATE is the backstop (C-3).

**Tech Stack:** Python 3 stdlib + jsonschema; pytest with the `lay` fixture (`tests/conftest.py`) and `tests/helpers.make_task` / `make_agdr`. Prompt protocols are markdown under `.claude/skills/sb-work/`.

**Grounding:** spec `docs/specs/2026-06-23-sb-escalation-design.md`; HDR-010 (three-tier escalation + independent-judge amendment), ADR-004 (intervention-learning), PHI-028/PHI-030. AgDRs ADR-008/009/010 (recorded with this plan) capture the C-1..C-4 calls for the gate.

**Design notes locked at plan time (covered by ADR-008/009/010, not re-litigated here):**
- **Tier authority is the verifier alone** — the author does not self-tier (C-2). Untagged `pending-review` AgDR ⇒ flag-async (fail-safe visible). So `task-protocol.md` is **unchanged** by this plan.
- **Tag vocabulary:** exactly `escalation:interrupt` and `escalation:record-silent` on the AgDR's `tags` array. Anything else (including no `escalation:` tag) ⇒ flag-async.
- **`sb resolve` re-queues** the task (`paused_for_human` → `queued`) and **preserves `attempts`** (the human unblocked a wedge; this is not a new failure). The optional resolution record is a `type:"human"` decision (`HDR-NNN`) whose **title is the one-line preventive rule** (the queryable index entry grounding surfaces), `context` = cause, `reasoning` = fix, `tags` include `intervention-resolution`. If no `--cause/--fix/--rule` is given, no record is written (optional by design — forcing it re-trains rubber-stamping).
- **Interrupt immediacy** is achieved by the SKILL calling `sb notify` right after the verify pass — no new engine path; `notify` is edge-triggered and already idempotent.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `schemas/digest.schema.json` | add `interrupt_agdrs` + `record_silent_agdrs`; bump const 0.1.0→0.2.0 | Modify |
| `sb/digest.py` | partition pending AgDRs by `escalation:` tag | Modify |
| `sb/notify.py` | ping `interrupt_agdrs` (immediate-class); never ping `record_silent_agdrs` | Modify |
| `sb/resolve.py` | `sb resolve` engine: re-queue paused task + optional resolution record | Create |
| `sb/cli.py` | wire the `resolve` subcommand | Modify |
| `tests/test_digest.py` | partitioning by escalation tag | Modify |
| `tests/test_digest_schema.py` | new buckets accepted; version const | Modify |
| `tests/test_notify.py` | interrupt fires, record-silent never fires | Modify |
| `tests/test_resolve.py` | re-queue + optional record + rejections | Create |
| `tests/test_cli.py` | `sb resolve` CLI | Modify |
| `.claude/skills/sb-work/verifier-protocol.md` | tier-calibration section | Modify (reviewed) |
| `.claude/skills/sb-work/SKILL.md` | immediate `sb notify` on interrupt | Modify (reviewed) |
| `docs/ROADMAP.md`, `CLAUDE.md` | status: C implemented | Modify |

Tasks 1–2 and 5–6 are TDD (engine). Tasks 3–4 are **reviewed-not-tested prose** (prompt protocols — spec §7; exercised live in D), each with a review checklist instead of a pytest step. Phase 1 = Tasks 1–4; Phase 2 = Tasks 5–6 (independent; may be done first).

---

## Phase 1 — tier calibration

### Task 1: digest partitions pending AgDRs by escalation tag (engine)

**Files:**
- Modify: `sb/digest.py`
- Modify: `schemas/digest.schema.json`
- Test: `tests/test_digest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_digest.py — add (imports at top: os, from sb import digest, store; from tests.helpers import make_agdr)
def _put_decision(lay, rec):
    store.write_json(os.path.join(lay.decisions, rec["id"] + ".json"), rec)

def test_digest_partitions_agdrs_by_escalation_tag(lay):
    cfg = {}
    _put_decision(lay, make_agdr(rec_id="ADR-201"))                                  # untagged -> flag-async
    _put_decision(lay, make_agdr(rec_id="ADR-202", tags=["escalation:interrupt"]))
    _put_decision(lay, make_agdr(rec_id="ADR-203", tags=["escalation:record-silent"]))
    dg = digest.build_digest(lay, cfg)
    assert [a["id"] for a in dg["pending_agdrs"]] == ["ADR-201"]
    assert [a["id"] for a in dg["interrupt_agdrs"]] == ["ADR-202"]
    assert [a["id"] for a in dg["record_silent_agdrs"]] == ["ADR-203"]

def test_non_pending_agdr_is_not_partitioned(lay):
    _put_decision(lay, make_agdr(rec_id="ADR-204", status="approved",
                                 tags=["escalation:interrupt"]))
    dg = digest.build_digest(lay, {})
    assert dg["pending_agdrs"] == [] and dg["interrupt_agdrs"] == []
    assert dg["record_silent_agdrs"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_digest.py::test_digest_partitions_agdrs_by_escalation_tag -v`
Expected: FAIL with `KeyError: 'interrupt_agdrs'` (and the schema would reject the new keys until Step 3).

- [ ] **Step 3: Bump the digest schema and add the two buckets**

In `schemas/digest.schema.json`: change `"schema_version": { "const": "0.1.0" }` to `"const": "0.2.0"`. Add `"interrupt_agdrs"` and `"record_silent_agdrs"` to the root `required` array. Add two properties identical in shape to `pending_agdrs` (copy its `items` object verbatim):

```json
"interrupt_agdrs": {
  "type": "array",
  "description": "HDR-010 tier-1: AgDRs the verifier tagged escalation:interrupt — pinged immediately by the worker after the verify pass.",
  "items": { "type": "object", "additionalProperties": false, "required": ["id"],
    "properties": { "id": {"type": "string"}, "title": {"type": ["string","null"]},
      "confidence": {"type": ["string","null"]}, "blast_radius": {"type": ["string","null"]},
      "provenance": {"type": "object"} } }
},
"record_silent_agdrs": {
  "type": "array",
  "description": "HDR-010 tier-3: AgDRs tagged escalation:record-silent — never pinged; present only in the gate-review profile.",
  "items": { "type": "object", "additionalProperties": false, "required": ["id"],
    "properties": { "id": {"type": "string"}, "title": {"type": ["string","null"]},
      "confidence": {"type": ["string","null"]}, "blast_radius": {"type": ["string","null"]},
      "provenance": {"type": "object"} } }
}
```

- [ ] **Step 4: Implement the partitioning in `sb/digest.py`**

Add a helper above `build_digest`:

```python
def _escalation_tier(decision):
    tags = decision.get("tags") or []
    if "escalation:interrupt" in tags:
        return "interrupt"
    if "escalation:record-silent" in tags:
        return "record-silent"
    return "flag-async"


def _agdr_view(d):
    return {"id": d.get("id"), "title": d.get("title"),
            "confidence": d.get("confidence"),
            "blast_radius": d.get("blast_radius"),
            "provenance": d.get("provenance", {})}
```

Replace the existing `pending_agdrs = [ ... ]` comprehension with:

```python
    pending = [d for d in _load_decisions(lay)
               if d.get("status") == "pending-review"]
    interrupt_agdrs = [_agdr_view(d) for d in pending
                       if _escalation_tier(d) == "interrupt"]
    record_silent_agdrs = [_agdr_view(d) for d in pending
                           if _escalation_tier(d) == "record-silent"]
    pending_agdrs = [_agdr_view(d) for d in pending
                     if _escalation_tier(d) == "flag-async"]
```

In the `digest = { ... }` dict: set `"schema_version": "0.2.0"`, and add `"interrupt_agdrs": interrupt_agdrs,` and `"record_silent_agdrs": record_silent_agdrs,` next to `"pending_agdrs": pending_agdrs,`.

- [ ] **Step 5: Update the digest-schema test for the new buckets + version**

Run: `pytest tests/test_digest_schema.py -v`. If it asserts the version const `"0.1.0"` or enumerates required keys, update those assertions to `"0.2.0"` and include `interrupt_agdrs` / `record_silent_agdrs`. Add:

```python
# tests/test_digest_schema.py
def test_digest_has_three_agdr_buckets(lay):
    from sb import digest
    dg = digest.build_digest(lay, {})
    for k in ("pending_agdrs", "interrupt_agdrs", "record_silent_agdrs"):
        assert isinstance(dg[k], list)
    assert dg["schema_version"] == "0.2.0"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_digest.py tests/test_digest_schema.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add sb/digest.py schemas/digest.schema.json tests/test_digest.py tests/test_digest_schema.py
git commit -m "feat(sb): digest partitions pending AgDRs by escalation tier (3-C)"
```

### Task 2: notify pings interrupt, never pings record-silent (engine)

**Files:**
- Modify: `sb/notify.py`
- Test: `tests/test_notify.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_notify.py — add (import: from sb import notify)
def _dg_with(**buckets):
    base = {"gates_ready": [], "paused_for_human": [], "pending_agdrs": [],
            "interrupt_agdrs": [], "record_silent_agdrs": [],
            "stale_workers": [], "quota": {"state": "ok"}}
    base.update(buckets)
    return base

def test_interrupt_agdr_fires_notification():
    dg = _dg_with(interrupt_agdrs=[{"id": "ADR-202", "title": "froze a contract",
                                    "confidence": "low"}])
    events = notify.collect_events(dg, seen=set())
    assert any(e["kind"] == "interrupt_agdr" and "ADR-202" in e["body"] for e in events)

def test_record_silent_agdr_never_fires():
    dg = _dg_with(record_silent_agdrs=[{"id": "ADR-203", "title": "renamed a local",
                                        "confidence": "high"}])
    events = notify.collect_events(dg, seen=set())
    assert events == []

def test_flag_async_agdr_still_fires():
    dg = _dg_with(pending_agdrs=[{"id": "ADR-201", "title": "chose snapshots",
                                  "confidence": "medium"}])
    events = notify.collect_events(dg, seen=set())
    assert any(e["kind"] == "pending_agdr" for e in events)
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_notify.py::test_interrupt_agdr_fires_notification -v`
Expected: FAIL (no `interrupt_agdr` event emitted).

- [ ] **Step 3: Implement in `sb/notify.py`**

In `_all_keys`, immediately after the `for a in dg.get("pending_agdrs", []):` block, add an interrupt block (do **not** add any block for `record_silent_agdrs` — its omission is what keeps it silent):

```python
    for a in dg.get("interrupt_agdrs", []):
        out.append((_key("interrupt_agdr", a["id"]), "interrupt_agdr",
                    "AgDR needs immediate review",
                    f"{a['id']}: {a.get('title') or ''} "
                    f"({a.get('confidence') or '?'} confidence)"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_notify.py -v`
Expected: PASS (including the existing flag-async test).

- [ ] **Step 5: Commit**

```bash
git add sb/notify.py tests/test_notify.py
git commit -m "feat(sb): notify pings interrupt AgDRs immediately, never record-silent (3-C)"
```

### Task 3: verifier-protocol tier-calibration section (reviewed-not-tested)

**Files:**
- Modify: `.claude/skills/sb-work/verifier-protocol.md`

- [ ] **Step 1: Add the calibration section**

After the existing verdict (pass/fail) instructions, add a section titled `## After the verdict — calibrate any AgDRs (HDR-010 tier-3)`. It must instruct the verifier subagent:

```markdown
## After the verdict — calibrate AgDRs (HDR-010)

This runs ONLY after you have decided the pass/fail verdict above; the verdict is
primary, this is secondary. If the task under verification listed AgDRs in its
result `decisions_emitted`, read each one in `decisions/<id>.json` and judge its
escalation tier by substance — confidence × blast-radius × reversibility:

- **interrupt** — a contestable call the author should arguably have stopped on
  (touches a frozen contract / security / a hard-to-reverse path, yet proceeded).
  Add the tag `escalation:interrupt` to the record's `tags` array.
- **record-silent** — high confidence, local blast, clearly reversible: routine.
  Add the tag `escalation:record-silent`.
- **flag-async** — anything in between (the default). Add **no** escalation tag.

Edit the record JSON in place (append the tag to `tags`; do not remove existing
tags), then `git add decisions/<id>.json` and include it in your commit on this
branch. Do NOT calibrate any AgDR you authored yourself (no self-judging). If you
are unsure, leave it untagged (flag-async) — the operator will still see it.
```

- [ ] **Step 2: Review checklist (no pytest — prompt protocol)**

Confirm by reading the edited file:
- The calibration is explicitly ordered **after** the verdict and labelled secondary (attention-dilution mitigation, spec C-1).
- Only the two tag strings `escalation:interrupt` / `escalation:record-silent` appear; flag-async ⇒ no tag (matches `_escalation_tier` in Task 1).
- The instruction is "append to `tags`, don't remove" and "commit on this branch" (so the tag travels with the AgDR — spec §6).
- The no-self-judging and "unsure ⇒ untagged" fail-safe lines are present.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/verifier-protocol.md
git commit -m "feat(sb-work): verifier calibrates AgDR escalation tier (3-C, reviewed-not-tested)"
```

### Task 4: SKILL immediate-notify on interrupt (reviewed-not-tested)

**Files:**
- Modify: `.claude/skills/sb-work/SKILL.md`

- [ ] **Step 1: Wire the immediate ping into the verify-pass step**

In the loop's "File the result" step (step 7), where a **verifier** subagent's pass is filed, add a sub-bullet:

```markdown
   - **After filing a verifier `pass`:** if the verifier added an
     `escalation:interrupt` tag to any AgDR it calibrated (check the AgDRs listed
     in the verified task's `decisions_emitted`, or just run it unconditionally —
     `notify` is edge-triggered and idempotent), run `sb notify --worker-id
     $WORKER_ID` once so the interrupt reaches the operator now rather than at the
     next periodic digest. flag-async and record-silent need no action here — the
     periodic monitor digest handles them.
```

- [ ] **Step 2: Review checklist**

- The ping fires only on a verifier **pass** path (not on task dispatch or fail).
- It calls the existing `sb notify` verb — no new engine surface (spec C-3).
- The note that `notify` is idempotent/edge-triggered is present (so an unconditional call is safe).
- record-silent / flag-async are explicitly **not** pinged here.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/sb-work/SKILL.md
git commit -m "feat(sb-work): worker fires immediate notify on interrupt-tier AgDR (3-C)"
```

---

## Phase 2 — `sb resolve` (independent of Phase 1)

### Task 5: `sb resolve` engine verb (engine)

**Files:**
- Create: `sb/resolve.py`
- Test: `tests/test_resolve.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_resolve.py
import os
import pytest
from sb import resolve, store, validate
from tests.helpers import make_task


def _paused(lay, task_id="PLAN-001/PH-1/T-1"):
    t = make_task(task_id=task_id, status="paused_for_human")
    t["attempts"] = 2
    store.write_task(lay, "paused", t)
    return t


def test_resolve_requeues_paused_task(lay):
    _paused(lay)
    resolve.resolve(lay, {}, "PLAN-001/PH-1/T-1")
    lane, task = store.find_task(lay, "PLAN-001/PH-1/T-1")
    assert lane == "queued"
    assert task["status"] == "queued"
    assert task["attempts"] == 2            # preserved
    assert "claim" not in task


def test_resolve_rejects_non_paused(lay):
    store.write_task(lay, "queued", make_task(task_id="PLAN-001/PH-1/T-2"))
    with pytest.raises(ValueError):
        resolve.resolve(lay, {}, "PLAN-001/PH-1/T-2")


def test_resolve_writes_optional_record(lay):
    _paused(lay)
    rec_id = resolve.resolve(lay, {"operator": "colin"}, "PLAN-001/PH-1/T-1",
                             cause="kept re-running the same failing migration",
                             fix="pinned the schema version first",
                             rule="pin the schema version before migrating")
    assert rec_id and rec_id.startswith("HDR-")
    rec = store.read_json(os.path.join(lay.decisions, rec_id + ".json"))
    validate.check("decision", rec)
    assert rec["title"] == "pin the schema version before migrating"
    assert "intervention-resolution" in rec["tags"]
    assert rec["provenance"]["task_id"] == "T-1"


def test_resolve_without_substance_writes_no_record(lay):
    _paused(lay)
    rec_id = resolve.resolve(lay, {}, "PLAN-001/PH-1/T-1")
    assert rec_id is None
    assert [f for f in os.listdir(lay.decisions) if f.endswith(".json")] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_resolve.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sb.resolve'`.

- [ ] **Step 3: Implement `sb/resolve.py`**

```python
"""sb resolve — the human-resolution path for a paused_for_human task (ADR-004).

Re-queues the task for a grounded retry and OPTIONALLY captures the resolution
as a tagged `human` decision record whose title is the one-line preventive rule.
That record flows into the `sb query` grounding the retrying author pulls, so the
fleet stops re-walking the same wall. The record is optional on purpose: forcing
a structured write on every un-pause re-trains the rubber-stamping HDR-010 fights.
"""

import datetime as dt
import os

from sb import leases, store, validate


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _next_decision_id(lay, prefix):
    n = 0
    if os.path.isdir(lay.decisions):
        for f in os.listdir(lay.decisions):
            if f.startswith(prefix + "-") and f.endswith(".json"):
                try:
                    n = max(n, int(f[len(prefix) + 1:-5]))
                except ValueError:
                    continue
    return f"{prefix}-{n + 1:03d}"


def _resolution_record(lay, cfg, task, cause, fix, rule):
    rec = {
        "schema_version": "0.3.0",
        "id": _next_decision_id(lay, "HDR"),
        "type": "human",
        "status": "approved",
        "timestamp": now_iso(),
        "level": "task",
        "tags": ["intervention-resolution"],
        "author": {"kind": "human", "id": cfg.get("operator", "operator"),
                   "role": "director"},
        "title": (rule or fix or cause or "intervention resolution")[:140],
        "context": cause or "",
        "reasoning": fix or "",
        "provenance": task.get("source", {}),
    }
    validate.check("decision", rec)
    store.write_json(os.path.join(lay.decisions, rec["id"] + ".json"), rec)
    return rec["id"]


def resolve(lay, cfg, task_id, cause=None, fix=None, rule=None):
    lane, task = store.find_task(lay, task_id)
    if lane != "paused" or task.get("status") != "paused_for_human":
        where = f"lane={lane}, status={task.get('status') if task else None}"
        raise ValueError(f"{task_id} is not paused_for_human ({where}); "
                         f"resolve only re-queues a human-paused task")

    rec_id = None
    if cause or fix or rule:
        rec_id = _resolution_record(lay, cfg, task, cause, fix, rule)

    # Re-queue for a grounded retry. attempts preserved (human unblocked a wedge,
    # not a new failure). write-before-move: finalize the body BEFORE the rename
    # into the claimable queued lane (ghost-task invariant).
    task["status"] = "queued"
    task.pop("claim", None)
    store.write_task(lay, "paused", task)
    if not store.move_task(lay, "paused", "queued", task_id):
        raise ValueError(f"{task_id} vanished from paused while resolving")
    leases.clear_lease(lay, task_id)
    return rec_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sb/resolve.py tests/test_resolve.py
git commit -m "feat(sb): sb resolve — re-queue paused_for_human + optional learning record (3-C, ADR-004)"
```

### Task 6: wire the `resolve` CLI verb (engine)

**Files:**
- Modify: `sb/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py — add (uses the same harness as the other cli tests in this file)
def test_cli_resolve_requeues(lay, monkeypatch):
    from sb import cli, store
    from tests.helpers import make_task
    t = make_task(task_id="PLAN-001/PH-1/T-9", status="paused_for_human")
    store.write_task(lay, "paused", t)
    rc = cli.main(["--repo", str(lay.repo), "resolve", "PLAN-001/PH-1/T-9",
                   "--rule", "always pin the schema first"])
    assert rc == 0
    lane, _ = store.find_task(lay, "PLAN-001/PH-1/T-9")
    assert lane == "queued"
```

(If the other tests in `tests/test_cli.py` pass `--repo` differently or use a helper to invoke `cli.main`, mirror that existing pattern instead of the literal above.)

- [ ] **Step 2: Run to verify it fails**

Run: `pytest tests/test_cli.py::test_cli_resolve_requeues -v`
Expected: FAIL (`invalid choice: 'resolve'` from argparse).

- [ ] **Step 3: Implement the CLI wiring in `sb/cli.py`**

Add the subparser next to the `block` one (after its `--reason` line):

```python
    p = common(sub.add_parser("resolve"))
    p.add_argument("task_id")
    p.add_argument("--cause", default=None)
    p.add_argument("--fix", default=None)
    p.add_argument("--rule", default=None)
```

Add `resolve` to the imports at the top of `cli.py` (alongside `results`, e.g. `from sb import ..., resolve, ...`). Add the dispatch branch next to the `block` branch:

```python
    if a.cmd == "resolve":
        rec_id = resolve.resolve(lay, cfg, a.task_id,
                                 cause=a.cause, fix=a.fix, rule=a.rule)
        _out({"task_id": a.task_id, "lane": "queued", "record": rec_id})
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_cli.py::test_cli_resolve_requeues -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sb/cli.py tests/test_cli.py
git commit -m "feat(sb): wire sb resolve CLI verb (3-C)"
```

---

## Task 7: full suite + docs

**Files:**
- Modify: `docs/ROADMAP.md`, `CLAUDE.md`

- [ ] **Step 1: Run the whole suite**

Run: `pytest -q`
Expected: all green (the prior 192 + the new digest/notify/resolve/cli tests).

- [ ] **Step 2: Update status docs**

In `docs/ROADMAP.md`, set the Plan 3-C row to **IMPLEMENTED** with the test count and a one-line summary (verifier tier calibration via `escalation:` tags; digest 3-bucket partition; immediate-notify on interrupt; `sb resolve`). In `CLAUDE.md`, add a State bullet mirroring the other sub-plan entries and add `resolve` to the engine-surface line.

- [ ] **Step 3: Commit**

```bash
git add docs/ROADMAP.md CLAUDE.md
git commit -m "docs(sb): Plan 3-C implemented — escalation tier calibration + sb resolve"
```

---

## Self-review (run after writing; checklist, not a dispatch)

- **Spec coverage:** §2 C-1 (Task 3 verifier calibration) · C-2 (no task-protocol change — design note) · C-3 (Task 2 + Task 4 immediate ping, task still done) · C-4 (Task 1 `escalation:` tag, no schema bump) · §3 digest (Task 1) · §3 notify (Task 2) · §3 `sb resolve` (Tasks 5–6) · §5 fail-safe untagged⇒flag-async (Task 1 `_escalation_tier` default + Task 3 "unsure ⇒ untagged") · §7 invariants (Tasks 1,5 tests). The inherited checkout-visibility seam (§6) is explicitly **out of scope** — no task, by design.
- **Placeholder scan:** none — every code/test step carries full code; the two "mirror existing pattern" notes (digest-schema version assertion in Task 1 Step 5, cli harness in Task 6 Step 1) are concrete fallbacks, not TODOs.
- **Type consistency:** `_escalation_tier`/`_agdr_view` (Task 1) ↔ tag strings in `verifier-protocol.md` (Task 3) ↔ `interrupt_agdrs`/`record_silent_agdrs` keys (Tasks 1,2) ↔ `resolve.resolve(lay, cfg, task_id, cause, fix, rule)` (Tasks 5,6) all match.
```
