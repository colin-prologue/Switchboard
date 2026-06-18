import os

import pytest

from sb import claims, leases, store
from sb.paths import LANES
from tests.helpers import make_task


def seed(lay, *tasks, lane="queued"):
    for t in tasks:
        store.write_task(lay, lane, t)


def test_release_requeues_active_task_attempts_unchanged(lay):
    seed(lay, make_task("PLAN-001/PH-1/T-1"))
    got = claims.claim_one(lay, "w1")
    assert got["attempts"] == 0
    assert store.find_task(lay, got["id"])[0] == "active"

    dest = claims.release(lay, got["id"])

    assert dest == "queued"
    lane, t = store.find_task(lay, got["id"])
    assert lane == "queued"
    assert t["status"] == "queued"
    assert t["attempts"] == 0          # infra requeue: attempts UNCHANGED
    assert "claim" not in t            # claim dropped
    assert leases.read_lease(lay, got["id"]) is None  # lease dropped


def test_release_leaves_exactly_one_file(lay):
    seed(lay, make_task())
    got = claims.claim_one(lay, "w1")
    claims.release(lay, got["id"])
    hits = [lane for lane in LANES
            if os.path.exists(store.task_path(lay, lane, got["id"]))]
    assert hits == ["queued"]


def test_release_rejects_non_active(lay):
    seed(lay, make_task())  # task is in queued, never claimed
    with pytest.raises(ValueError, match="not active"):
        claims.release(lay, "PLAN-001/PH-1/T-1")


def test_release_rejects_unknown(lay):
    with pytest.raises(ValueError, match="not active"):
        claims.release(lay, "PLAN-001/PH-1/NOPE")


def test_cli_release(lay, capsys):
    import json

    from sb import cli
    seed(lay, make_task())
    claims.claim_one(lay, "w1")
    rc = cli.main(["release", "PLAN-001/PH-1/T-1", "--repo", lay.repo])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"task_id": "PLAN-001/PH-1/T-1", "lane": "queued"}
    assert store.find_task(lay, "PLAN-001/PH-1/T-1")[0] == "queued"
