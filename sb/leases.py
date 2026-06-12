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
