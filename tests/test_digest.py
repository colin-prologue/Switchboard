import os
import time

from sb import claims, digest, leases, store
from tests.helpers import make_agdr, make_task, put_decision


def test_empty_repo_digest_is_valid_and_quiet(lay):
    cfg = {"lease_ttl_s": 5400}
    d = digest.build_digest(lay, cfg)
    assert d["schema_version"] == "0.1.0"
    assert d["lanes"] == {"queued": 0, "active": 0, "paused": 0,
                          "done": 0, "failed": 0}
    assert d["gates_ready"] == [] and d["pending_agdrs"] == []
    assert d["quota"] == {"state": "ok"}


def test_lane_counts(lay):
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    store.write_task(lay, "done", make_task("PLAN-001/PH-1/T-2", status="done"))
    d = digest.build_digest(lay, {})
    assert d["lanes"]["queued"] == 1 and d["lanes"]["done"] == 1


def test_pending_agdrs_carried(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    put_decision(lay, make_agdr("ADR-052", status="approved"))
    d = digest.build_digest(lay, {})
    ids = [a["id"] for a in d["pending_agdrs"]]
    assert ids == ["ADR-051"]
    assert d["pending_agdrs"][0]["blast_radius"] == "Cache module only; no public API change."


def test_gate_ready_when_deps_done(lay):
    store.write_task(lay, "done", make_task("PLAN-001/PH-1/T-1", status="done"))
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human",
                     context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)
    d = digest.build_digest(lay, {})
    assert [g["id"] for g in d["gates_ready"]] == ["PLAN-001/PH-1/GATE"]


def test_gate_not_ready_when_deps_pending(lay):
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human",
                     context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)
    assert digest.build_digest(lay, {})["gates_ready"] == []


def test_paused_for_human_listed(lay):
    t = make_task("PLAN-001/PH-1/T-9", status="paused_for_human")
    t["failure"] = {"reason": "missing credential"}
    store.write_task(lay, "paused", t)
    d = digest.build_digest(lay, {})
    assert d["paused_for_human"] == [
        {"id": "PLAN-001/PH-1/T-9", "reason": "missing credential"}]


def test_stale_active_surfaces_expired_lease(lay):
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    claims.claim_one(lay, "w1", cfg={"lease_ttl_s": 100})
    lease = leases.read_lease(lay, "PLAN-001/PH-1/T-1")
    lease["claimed_at"] -= lease["ttl_s"] + 1
    store.write_json(leases.lease_path(lay, "PLAN-001/PH-1/T-1"), lease)
    d = digest.build_digest(lay, {"lease_ttl_s": 100})
    assert [s["id"] for s in d["stale_active"]] == ["PLAN-001/PH-1/T-1"]


def test_stale_workers_from_heartbeats(lay):
    claims.heartbeat(lay, "w1")
    rec_path = os.path.join(lay.heartbeats, "w1.json")
    rec = store.read_json(rec_path)
    rec["at"] = time.time() - 9000
    store.write_json(rec_path, rec)
    d = digest.build_digest(lay, {"lease_ttl_s": 5400})
    assert d["stale_workers"][0]["worker_id"] == "w1"


def test_quota_read_from_file(lay):
    store.write_json(os.path.join(lay.root, "quota.json"),
                     {"state": "exhausted", "retry_after_s": 600})
    d = digest.build_digest(lay, {})
    assert d["quota"]["state"] == "exhausted"
