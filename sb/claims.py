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
    requeue with attempts UNCHANGED.

    Body is cleaned BEFORE the rename: once the file lands in queued/ it is
    instantly claimable, so no write may follow the move (ghost-task race)."""
    requeued = []
    for t in store.list_tasks(lay, "active"):
        lease = leases.read_lease(lay, t["id"])
        if lease is not None and not leases.is_expired(lease):
            continue
        t["status"] = "queued"
        t.pop("claim", None)
        store.write_task(lay, "active", t)
        if not store.move_task(lay, "active", "queued", t["id"]):
            continue  # someone else swept or filed it first
        leases.clear_lease(lay, t["id"])
        requeued.append(t["id"])
    return requeued


def heartbeat(lay, worker_id):
    store.write_json(os.path.join(lay.heartbeats, store.fname(worker_id)),
                     {"worker_id": worker_id, "at": time.time()})
