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
        # Lease BEFORE the body write: a concurrent requeue-stale sweep keys off
        # the lease, so writing it first stops the sweep from seeing this
        # just-moved active file as lease-less and bouncing it back to queued
        # (which would leave the task in both lanes — duplicate dispatch). This
        # narrows the race to the rename→lease gap; it does not fully close it,
        # but nothing auto-runs the sweep today (Codex C3 mitigation).
        leases.write_lease(lay, t["id"], worker_id, ttl)
        t["status"] = "claimed"
        t["claim"] = {"worker_id": worker_id, "claimed_at": now_iso()}
        store.write_task(lay, "active", t)
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
    requeue with attempts UNCHANGED.

    Body is cleaned BEFORE the rename: once the file lands in queued/ it is
    instantly claimable, so no write may follow the move (ghost-task race).
    Lease cleared before the rename so a fresh claimer's lease is never
    clobbered."""
    requeued = []
    for t in store.list_tasks(lay, "active"):
        lease = leases.read_lease(lay, t["id"])
        if lease is not None and not leases.is_expired(lease):
            continue
        t["status"] = "queued"
        t.pop("claim", None)
        store.write_task(lay, "active", t)
        leases.clear_lease(lay, t["id"])
        if not store.move_task(lay, "active", "queued", t["id"]):
            continue  # someone else swept or filed it first
        requeued.append(t["id"])
    return requeued


def release(lay, task_id):
    """Infra-requeue a named active task: active -> queued, attempts UNCHANGED,
    lease dropped. The loop calls this when a dispatch raises a rate-limit /
    usage-cap signal (infra failure, not task failure — spec §5/§8). Unlike
    file-result's requeue path it never increments attempts; unlike
    requeue_stale it targets one task and does not consult the lease.

    Same ordering as requeue_stale: finalize the body while still in active/
    (un-claimable), clear the lease before the rename so a fresh claimer's lease
    is never clobbered, then the atomic move. Never write after the move.

    Caller contract: release is called by the worker loop for a task it currently
    holds (valid lease), so the concurrent-sweep race below is normally
    unreachable; the ghost-cleanup is defensive."""
    lane, task = store.find_task(lay, task_id)
    if lane != "active":
        where = f"lane={lane}" if lane else "not found in any lane"
        raise ValueError(f"{task_id} is not active ({where}); cannot release")
    task["status"] = "queued"
    task.pop("claim", None)
    store.write_task(lay, "active", task)
    leases.clear_lease(lay, task_id)
    if not store.move_task(lay, "active", "queued", task_id):
        # Swept out of active/ between find_task and here: our write_task
        # re-created a ghost in active/. Remove it before surfacing the race.
        try:
            os.remove(store.task_path(lay, "active", task_id))
        except FileNotFoundError:
            pass
        raise ValueError(f"{task_id} vanished from active while releasing")
    return "queued"


def heartbeat(lay, worker_id):
    store.write_json(os.path.join(lay.heartbeats, store.fname(worker_id)),
                     {"worker_id": worker_id, "at": time.time()})
