"""Status digest — the read-side primitive (spec §7). A pure function of disk
state, validated against schemas/digest.schema.json. The notify hook and the
future nexus both consume it. Carries pending-review AgDRs so HDR-010 tier-2
escalations reach the human through the notification channel."""

import datetime as dt
import os
import time

from sb import leases, store, validate
from sb.paths import LANES


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_decisions(lay):
    out = []
    if not os.path.isdir(lay.decisions):
        return out
    for f in sorted(os.listdir(lay.decisions)):
        if not f.endswith(".json"):
            continue
        try:
            out.append(store.read_json(os.path.join(lay.decisions, f)))
        except (ValueError, OSError):
            continue
    return out


def build_digest(lay, cfg, now=None):
    now = time.time() if now is None else now
    ttl = cfg.get("lease_ttl_s", 5400)

    lanes = {lane: len(store.list_tasks(lay, lane)) for lane in LANES}

    # Active tasks whose lease expired or vanished. This is where a verify task
    # with a malformed (verdict-less) result sits until the stale sweep requeues
    # it (see sb/results.py carry-over note) — surface it here.
    stale_active = []
    for t in store.list_tasks(lay, "active"):
        lease = leases.read_lease(lay, t["id"])
        if lease is None or leases.is_expired(lease, now=now):
            stale_active.append({"id": t["id"],
                                 "verifies": t.get("context", {}).get("verifies")})

    # Heartbeats older than the lease TTL => fleet stalled / silent session death.
    stale_workers = []
    if os.path.isdir(lay.heartbeats):
        for f in sorted(os.listdir(lay.heartbeats)):
            if not f.endswith(".json"):
                continue
            rec = store.read_json(os.path.join(lay.heartbeats, f))
            age = now - rec.get("at", 0)
            if age > ttl:
                stale_workers.append({"worker_id": rec.get("worker_id"),
                                      "last_seen_s_ago": round(age)})

    done = store.done_ids(lay)
    gates_ready, paused_for_human = [], []
    for t in store.list_tasks(lay, "paused"):
        deps = t.get("context", {}).get("depends_on", [])
        if t["id"].endswith("/GATE"):
            if all(d in done for d in deps):
                gates_ready.append({"id": t["id"],
                                    "condition": t.get("done", {}).get("statement")})
        elif t.get("status") == "paused_for_human":
            paused_for_human.append({"id": t["id"],
                                     "reason": t.get("failure", {}).get("reason", "")})

    pending_agdrs = [
        {"id": d.get("id"), "title": d.get("title"),
         "confidence": d.get("confidence"),
         "blast_radius": d.get("blast_radius"),
         "provenance": d.get("provenance", {})}
        for d in _load_decisions(lay)
        if d.get("status") == "pending-review"]

    # ADVISORY status hint only (HDR-011): a deterministic detector (Plan 3
    # PostToolUse hook) writes this token-free on a rate-limit signal; the
    # engine never gates a claim on it. Absent => healthy.
    qpath = os.path.join(lay.root, "quota.json")
    quota = store.read_json(qpath) if os.path.exists(qpath) else {"state": "ok"}

    digest = {
        "schema_version": "0.1.0",
        "generated_at": now_iso(),
        "lanes": lanes,
        "gates_ready": gates_ready,
        "paused_for_human": paused_for_human,
        "pending_agdrs": pending_agdrs,
        "stale_workers": stale_workers,
        "stale_active": stale_active,
        "quota": quota,
    }
    validate.check("digest", digest)
    return digest
