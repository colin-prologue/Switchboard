import os
import time

from sb import claims, leases, store
from tests.helpers import make_task


def seed(lay, *tasks, lane="queued"):
    for t in tasks:
        store.write_task(lay, lane, t)


def test_deps_met():
    t = make_task(context={"depends_on": ["A", "B"]})
    assert claims.deps_met(t, {"A", "B"})
    assert not claims.deps_met(t, {"A"})


def test_claim_respects_tier_and_deps(lay):
    seed(lay,
         make_task("PLAN-001/PH-1/T-1", tier="opus"),
         make_task("PLAN-001/PH-1/T-2", tier="haiku",
                   context={"depends_on": ["PLAN-001/PH-1/T-1"]}),
         make_task("PLAN-001/PH-1/T-3", tier="haiku"))
    got = claims.claim_one(lay, "w1", tier="haiku")
    assert got["id"] == "PLAN-001/PH-1/T-3"  # T-2 blocked, T-1 wrong tier


def test_claim_any_tier(lay):
    seed(lay, make_task("PLAN-001/PH-1/T-1", tier="opus"))
    got = claims.claim_one(lay, "w1")
    assert got["id"] == "PLAN-001/PH-1/T-1"


def test_claim_sets_state_and_lease_without_touching_attempts(lay):
    seed(lay, make_task())
    got = claims.claim_one(lay, "w1")
    assert got["status"] == "claimed"
    assert got["claim"]["worker_id"] == "w1"
    assert got["attempts"] == 0  # claims never count as attempts
    lane, on_disk = store.find_task(lay, got["id"])
    assert lane == "active"
    assert leases.read_lease(lay, got["id"])["worker_id"] == "w1"


def test_claim_returns_none_when_empty(lay):
    assert claims.claim_one(lay, "w1") is None


def test_claim_wait_returns_immediately_when_present(lay):
    seed(lay, make_task())
    start = time.monotonic()
    got = claims.claim_wait(lay, "w1", wait_s=5, poll_s=0.1)
    assert got is not None
    assert time.monotonic() - start < 1


def test_claim_wait_times_out(lay):
    start = time.monotonic()
    assert claims.claim_wait(lay, "w1", wait_s=0.3, poll_s=0.1) is None
    assert time.monotonic() - start >= 0.3


def test_requeue_stale_only_touches_expired(lay):
    fresh, stale = make_task("PLAN-001/PH-1/T-1"), make_task("PLAN-001/PH-1/T-2")
    seed(lay, fresh, stale)
    claims.claim_one(lay, "w1", )  # claims T-1
    claims.claim_one(lay, "w2")    # claims T-2
    # expire T-2's lease by rewriting it in the past
    lease = leases.read_lease(lay, stale["id"])
    lease["claimed_at"] -= lease["ttl_s"] + 1
    store.write_json(leases.lease_path(lay, stale["id"]), lease)

    requeued = claims.requeue_stale(lay, {"lease_ttl_s": 5400})
    assert requeued == [stale["id"]]
    lane, t = store.find_task(lay, stale["id"])
    assert lane == "queued" and t["status"] == "queued"
    assert "claim" not in t and t["attempts"] == 0
    assert store.find_task(lay, fresh["id"])[0] == "active"


def test_requeue_stale_handles_missing_lease(lay):
    seed(lay, make_task())
    got = claims.claim_one(lay, "w1")
    leases.clear_lease(lay, got["id"])
    assert claims.requeue_stale(lay, {}) == [got["id"]]


def test_heartbeat_touches_file(lay):
    claims.heartbeat(lay, "w1")
    p = os.path.join(lay.heartbeats, "w1.json")
    assert os.path.exists(p)
    assert store.read_json(p)["worker_id"] == "w1"
