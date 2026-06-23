import os

from sb import seed, store, validate


def test_seed_goal_creates_one_planner_task(lay):
    cid = seed.seed_goal(lay, "build a thing")
    assert cid == "PLAN-001/PH-0/T-1"
    lane, t = store.find_task(lay, cid)
    assert lane == "queued"
    assert t["goal"] == "build a thing"
    assert t["tier"] == "opus"
    assert t["context"]["branch"] == "sb/plan-001/ph-0"
    assert t["context"]["depends_on"] == []
    # The discriminator the worker loop routes on (ADR-007):
    assert t["done"]["verify"]["kind"] == "plan"
    assert t["done"]["verify"]["ref"] == "plans/PLAN-001.json"
    assert "plans/PLAN-001.json" in t["done"]["statement"]
    # No gate task is created for seed --goal:
    assert store.list_tasks(lay, "paused") == []


def test_seed_goal_task_is_schema_valid(lay):
    cid = seed.seed_goal(lay, "g")
    _, t = store.find_task(lay, cid)
    validate.check("task", t)  # raises if invalid


def test_seed_goal_first_id_is_plan_001(lay):
    assert seed.seed_goal(lay, "g").startswith("PLAN-001/")


def test_seed_goal_allocates_next_id_past_existing_plan_file(lay):
    store.write_json(os.path.join(lay.plans, "PLAN-001.json"), {})
    assert seed.seed_goal(lay, "g").startswith("PLAN-002/")


def test_seed_goal_allocates_next_id_past_in_flight_planner_task(lay):
    seed.seed_goal(lay, "g1")            # PLAN-001 planner task queued
    assert seed.seed_goal(lay, "g2").startswith("PLAN-002/")


def test_seed_goal_tier_override(lay):
    cid = seed.seed_goal(lay, "g", tier="fable")
    _, t = store.find_task(lay, cid)
    assert t["tier"] == "fable"


def test_seed_goal_repo_state_carried(lay):
    cid = seed.seed_goal(lay, "g", repo_state="deadbeef")
    _, t = store.find_task(lay, cid)
    assert t["context"]["repo_state"] == "deadbeef"
