import pytest

from sb import claims, seed, stamp, store
from tests.helpers import make_agdr, put_decision

PLAN = {
    "schema_version": "0.1.0", "plan_id": "PLAN-001", "goal": "toy goal",
    "created": "2026-06-14T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [{"task_id": "T-1", "title": "Choose the design",
                    "done": {"statement": "ADR exists"}}]},
        {"phase_id": "PH-2", "name": "Build", "default_model": "haiku",
         "tasks": [{"task_id": "T-2", "title": "Implement it",
                    "depends_on": ["T-1"],
                    "done": {"statement": "tests green"}}]},
    ],
}


def seed_and_finish_ph1(lay):
    """Seed the 2-phase plan and drive PH-1's T-1 to done."""
    seed.seed(lay, PLAN)
    store.move_task(lay, "queued", "done", "PLAN-001/PH-1/T-1")
    _, t = store.find_task(lay, "PLAN-001/PH-1/T-1")
    t["status"] = "done"
    store.write_task(lay, "done", t)


def test_approve_completes_gate_and_unblocks_next_phase(lay):
    seed_and_finish_ph1(lay)
    # PH-2/T-2 is blocked behind the un-stamped gate
    assert claims.claim_one(lay, "w1") is None

    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve",
                      note="LGTM", reviewer="colin")
    assert out["gate_advanced"] is True
    lane, gate = store.find_task(lay, "PLAN-001/PH-1/GATE")
    assert lane == "done" and gate["status"] == "done"

    # next phase is now claimable
    got = claims.claim_one(lay, "w1")
    assert got["id"] == "PLAN-001/PH-2/T-2"


def test_approve_writes_hdr_with_provenance(lay):
    seed_and_finish_ph1(lay)
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="ok")
    rec = store.read_json(f"{lay.decisions}/{out['hdr']}.json")
    assert rec["type"] == "human" and rec["id"].startswith("HDR-")
    assert rec["provenance"] == {"plan_id": "PLAN-001", "phase_id": "PH-1"}
    assert rec["status"] == "approved"


def test_approve_stamps_phase_agdrs(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review",
                                plan_id="PLAN-001", phase_id="PH-1"))
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="good call")
    assert out["touched"] == ["ADR-051"]
    rec = store.read_json(f"{lay.decisions}/ADR-051.json")
    assert rec["status"] == "feedback-incorporated"   # note present
    assert rec["feedback"][-1]["action"] == "approve"
    assert rec["feedback"][-1]["note"] == "good call"


def test_stamp_targets_one_decision(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    put_decision(lay, make_agdr("ADR-052", status="pending-review"))
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve",
                      note="", target="ADR-051")
    assert out["touched"] == ["ADR-051"]
    assert store.read_json(f"{lay.decisions}/ADR-052.json")["status"] == "pending-review"


def test_approve_without_note_marks_approved(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="")
    assert store.read_json(f"{lay.decisions}/ADR-051.json")["status"] == "approved"


def test_approve_whitespace_note_marks_approved_not_incorporated(lay):
    # a whitespace-only note is not substantive feedback -> plain approval
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="   ")
    assert store.read_json(f"{lay.decisions}/ADR-051.json")["status"] == "approved"


def test_revise_returns_decisions_to_proposed_and_keeps_gate_paused(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="revise",
                      note="reconsider memory budget")
    assert out["gate_advanced"] is False
    assert store.find_task(lay, "PLAN-001/PH-1/GATE")[0] == "paused"
    assert store.read_json(f"{lay.decisions}/ADR-051.json")["status"] == "proposed"


def test_approve_not_ready_raises_unless_forced(lay):
    seed.seed(lay, PLAN)   # T-1 still queued; gate not ready
    with pytest.raises(stamp.GateNotReady):
        stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="x")
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="x",
                      force=True)
    assert out["gate_advanced"] is True
