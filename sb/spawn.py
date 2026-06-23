"""Research handoff. A waiting parent NEVER holds a worker: the parent is
re-enqueued as its own continuation, gaining a dependency on the research
task and carrying its partial result forward (retries are never blind).

Crash window: the research task is written to queued/ before the parent
gains its depends_on edge. A crash in that window orphans the research task
(it runs, completes, unblocks nothing) and the parent recovers via lease
expiry and re-spawns under the next suffix. Accepted: bounded duplicate
work, no corruption."""

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


def _consume_partial(lay, parent):
    """Carry the parent's partial result file (if the session wrote one) into
    its prior_attempts, then remove the file. Runs on BOTH the spawn and the
    chain-depth-cap paths — a handoff filed at the cap must still preserve the
    partial (so the human sees why it stalled) and not orphan the result file
    (Codex C1, PR #3)."""
    rpath = os.path.join(lay.results, store.fname(parent["id"]))
    if not os.path.exists(rpath):
        return
    try:
        partial = store.read_json(rpath)
    except ValueError:
        partial = {"note": "partial result file was corrupt and was discarded"}
    parent.setdefault("context", {}).setdefault("prior_attempts", []).append(partial)
    os.remove(rpath)


def spawn_research(lay, cfg, parent_id, goal, tier, done_statement):
    lane, parent = store.find_task(lay, parent_id)
    if lane != "active":
        raise ValueError(f"{parent_id} is not active (lane={lane})")

    depth = parent.get("context", {}).get("chain_depth", 0) + 1
    if depth > cfg.get("max_chain_depth", 3):
        _consume_partial(lay, parent)  # preserve the partial; don't orphan rp
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

    _consume_partial(lay, parent)  # carry partial forward; retries are never blind

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
