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
    if "id" in over and "source" not in over:
        plan_id, phase_id, leaf = task["id"].split("/")[:3]
        task["source"] = {"plan_id": plan_id, "phase_id": phase_id,
                          "task_id": leaf.split(".")[0]}
    if ctx:
        task["context"] = {**task["context"], **ctx}
    return task


def make_agdr(rec_id="ADR-051", status="pending-review", plan_id="PLAN-001",
              phase_id="PH-1", **over):
    """Schema-valid (decision v0.3.0) AgDR for brief/stamp/digest tests."""
    rec = {
        "schema_version": "0.3.0",
        "id": rec_id,
        "type": "agent",
        "status": status,
        "timestamp": "2026-06-14T00:00:00+00:00",
        "level": "component",
        "tags": ["caching"],
        "author": {"kind": "model", "id": "claude-opus-4-8", "role": "coder"},
        "title": "Use immutable snapshots for the cache",
        "context": "Concurrency model had to be chosen before implementation.",
        "options": [
            {"name": "immutable-snapshots", "rationale": "no locks"},
            {"name": "mutable-with-locks", "rationale": "lower memory"},
        ],
        "chosen": "immutable-snapshots",
        "confidence": "medium",
        "reasoning": "Snapshots remove the lock-contention failure mode entirely.",
        "steelman": [{"option": "mutable-with-locks",
                      "strongest_case": "Lower memory; a familiar pattern."}],
        "blast_radius": "Cache module only; no public API change.",
        "evidence": [{"kind": "test", "ref": "tests/test_cache.py", "result": "pass"}],
        "provenance": {"plan_id": plan_id, "phase_id": phase_id, "task_id": "T-1"},
    }
    rec.update(over)
    return rec


def put_decision(lay, rec):
    import json
    import os
    with open(os.path.join(lay.decisions, f"{rec['id']}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f)
