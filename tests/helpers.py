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
