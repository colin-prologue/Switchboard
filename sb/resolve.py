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
