import os
import pytest
from sb import resolve, store, validate
from tests.helpers import make_task


def _paused(lay, task_id="PLAN-001/PH-1/T-1"):
    t = make_task(task_id=task_id, status="paused_for_human")
    t["attempts"] = 2
    store.write_task(lay, "paused", t)
    return t


def test_resolve_requeues_paused_task(lay):
    _paused(lay)
    resolve.resolve(lay, {}, "PLAN-001/PH-1/T-1")
    lane, task = store.find_task(lay, "PLAN-001/PH-1/T-1")
    assert lane == "queued"
    assert task["status"] == "queued"
    assert task["attempts"] == 2            # preserved
    assert "claim" not in task


def test_resolve_rejects_gate(lay):
    store.write_task(lay, "paused",
                     make_task(task_id="PLAN-001/PH-1/GATE", status="paused_for_human"))
    with pytest.raises(ValueError):
        resolve.resolve(lay, {}, "PLAN-001/PH-1/GATE")


def test_resolve_rejects_non_paused(lay):
    store.write_task(lay, "queued", make_task(task_id="PLAN-001/PH-1/T-2"))
    with pytest.raises(ValueError):
        resolve.resolve(lay, {}, "PLAN-001/PH-1/T-2")


def test_resolve_writes_optional_record(lay):
    _paused(lay)
    rec_id = resolve.resolve(lay, {"operator": "colin"}, "PLAN-001/PH-1/T-1",
                             cause="kept re-running the same failing migration",
                             fix="pinned the schema version first",
                             rule="pin the schema version before migrating")
    assert rec_id and rec_id.startswith("HDR-")
    rec = store.read_json(os.path.join(lay.decisions, rec_id + ".json"))
    validate.check("decision", rec)
    assert rec["title"] == "pin the schema version before migrating"
    assert "intervention-resolution" in rec["tags"]
    assert rec["provenance"]["task_id"] == "T-1"


def test_resolve_without_substance_writes_no_record(lay):
    _paused(lay)
    rec_id = resolve.resolve(lay, {}, "PLAN-001/PH-1/T-1")
    assert rec_id is None
    assert [f for f in os.listdir(lay.decisions) if f.endswith(".json")] == []
