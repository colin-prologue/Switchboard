import json

import pytest

from sb import seed, store

PLAN = {
    "schema_version": "0.1.0",
    "plan_id": "PLAN-001",
    "goal": "toy goal",
    "created": "2026-06-12T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "constraints": ["no new deps"],
    "grounding": ["HDR-006"],
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [
             {"task_id": "T-1", "title": "Choose the design",
              "done": {"statement": "ADR exists"}},
         ]},
        {"phase_id": "PH-2", "name": "Build", "default_model": "haiku",
         "tasks": [
             {"task_id": "T-2", "title": "Implement it",
              "depends_on": ["T-1"],
              "done": {"statement": "tests green"}},
         ]},
    ],
}


def test_seed_creates_tasks_with_branch_and_grounding(lay):
    seeded = seed.seed(lay, PLAN, repo_state="abc123")
    assert "PLAN-001/PH-1/T-1" in seeded
    _, t1 = store.find_task(lay, "PLAN-001/PH-1/T-1")
    assert t1["tier"] == "opus"
    assert t1["context"]["branch"] == "sb/plan-001/ph-1"
    assert t1["context"]["repo_state"] == "abc123"
    assert t1["context"]["grounding"] == ["HDR-006"]
    assert t1["context"]["chain_depth"] == 0


def test_every_phase_gets_a_gate_and_next_phase_blocks_on_it(lay):
    seed.seed(lay, PLAN)
    lane, gate1 = store.find_task(lay, "PLAN-001/PH-1/GATE")
    assert lane == "paused" and gate1["status"] == "paused_for_human"
    assert gate1["context"]["depends_on"] == ["PLAN-001/PH-1/T-1"]

    _, t2 = store.find_task(lay, "PLAN-001/PH-2/T-2")
    assert set(t2["context"]["depends_on"]) == {
        "PLAN-001/PH-1/T-1", "PLAN-001/PH-1/GATE"}

    lane, gate2 = store.find_task(lay, "PLAN-001/PH-2/GATE")
    assert lane == "paused"  # final phase is gated too: the PR-gate invariant


def test_blocking_questions_hold_seed(lay):
    plan = dict(PLAN, open_questions=[
        {"question": "Which SLA?", "blocking": True, "resolve_by": "human"}])
    with pytest.raises(seed.BlockingQuestions, match="Which SLA"):
        seed.seed(lay, plan)
    assert store.list_tasks(lay, "queued") == []


def test_force_overrides_blocking_questions(lay):
    plan = dict(PLAN, open_questions=[
        {"question": "Which SLA?", "blocking": True, "resolve_by": "human"}])
    seeded = seed.seed(lay, plan, force=True)
    assert len(seeded) == 2


def test_reseeding_same_plan_refuses(lay):
    seed.seed(lay, PLAN)
    with pytest.raises(seed.AlreadySeeded, match="PLAN-001"):
        seed.seed(lay, PLAN)


def test_forward_dep_rejected(lay):
    bad = json.loads(json.dumps(PLAN))
    bad["phases"][0]["tasks"][0]["depends_on"] = ["T-2"]  # T-2 lives in PH-2
    with pytest.raises(ValueError, match="later phase"):
        seed.seed(lay, bad)


def test_forward_dep_rejected_before_any_write(lay):
    bad = json.loads(json.dumps(PLAN))
    bad["phases"][1]["tasks"][0]["depends_on"] = ["T-1", "T-9"]
    bad["phases"][1]["tasks"].append(
        {"task_id": "T-9", "title": "late", "done": {"statement": "d"}})
    bad["phases"][0]["tasks"][0]["depends_on"] = ["T-2"]  # forward dep, phase 1
    with pytest.raises(ValueError, match="later phase"):
        seed.seed(lay, bad)
    assert store.list_tasks(lay, "queued") == []
    assert store.list_tasks(lay, "paused") == []
