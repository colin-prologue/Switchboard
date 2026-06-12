import pytest

from sb import store
from tests.helpers import make_task


def test_write_read_roundtrip(lay):
    t = make_task()
    store.write_task(lay, "queued", t)
    assert store.read_json(store.task_path(lay, "queued", t["id"])) == t


def test_write_task_validates(lay):
    bad = make_task()
    bad["status"] = "nope"
    with pytest.raises(ValueError):
        store.write_task(lay, "queued", bad)


def test_fname_escapes_slashes():
    assert store.fname("PLAN-001/PH-1/T-1") == "PLAN-001_PH-1_T-1.json"


def test_move_task_is_atomic_and_loses_race(lay):
    t = make_task()
    store.write_task(lay, "queued", t)
    assert store.move_task(lay, "queued", "active", t["id"]) is True
    # second mover finds the source gone — the lost race
    assert store.move_task(lay, "queued", "active", t["id"]) is False


def test_find_task_scans_lanes(lay):
    t = make_task()
    store.write_task(lay, "paused", t)
    lane, found = store.find_task(lay, t["id"])
    assert lane == "paused" and found["id"] == t["id"]
    assert store.find_task(lay, "PLAN-999/PH-9/T-9") == (None, None)


def test_list_tasks_and_done_ids(lay):
    a = make_task("PLAN-001/PH-1/T-1")
    b = make_task("PLAN-001/PH-1/T-2", status="done")
    store.write_task(lay, "queued", a)
    store.write_task(lay, "done", b)
    assert [t["id"] for t in store.list_tasks(lay, "queued")] == [a["id"]]
    assert store.done_ids(lay) == {b["id"]}
