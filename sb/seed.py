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
