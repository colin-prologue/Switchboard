import json

import pytest
from jsonschema import Draft202012Validator

SCHEMAS = "schemas"


def load(name):
    with open(f"{SCHEMAS}/{name}", encoding="utf-8") as f:
        return Draft202012Validator(json.load(f))


GOOD_RESULT = {
    "schema_version": "0.1.0",
    "outcome": "success",
    "summary": "Implemented the parser; tests green.",
    "evidence": [{"kind": "test", "ref": "tests/test_parser.py", "result": "pass"}],
    "decisions_emitted": ["ADR-051"],
    "completed_at": "2026-06-12T00:00:00+00:00",
}


def test_result_schema_accepts_valid():
    load("result.schema.json").validate(GOOD_RESULT)


def test_result_schema_accepts_verdict():
    r = dict(GOOD_RESULT, verdict="fail", verdict_notes="stress test flaked twice")
    load("result.schema.json").validate(r)


def test_result_schema_rejects_unknown_field():
    v = load("result.schema.json")
    assert not v.is_valid(dict(GOOD_RESULT, surprise=1))


def test_result_schema_rejects_bad_outcome():
    v = load("result.schema.json")
    assert not v.is_valid(dict(GOOD_RESULT, outcome="meh"))


def test_task_schema_accepts_v2_context_fields():
    task = {
        "schema_version": "0.2.0",
        "id": "PLAN-001/PH-1/T-1",
        "tier": "haiku",
        "status": "awaiting_verification",
        "source": {"plan_id": "PLAN-001", "phase_id": "PH-1", "task_id": "T-1"},
        "goal": "do the thing",
        "context": {
            "repo_state": "HEAD",
            "branch": "sb/plan-001/ph-1",
            "chain_depth": 1,
            "verifies": "PLAN-001/PH-1/T-0",
            "prior_attempts": [{"anything": "goes here"}],
            "depends_on": [],
        },
        "done": {"statement": "thing is done"},
        "attempts": 0,
        "created_at": "2026-06-12T00:00:00+00:00",
        "created_by": "test",
    }
    load("task.schema.json").validate(task)


def test_task_schema_accepts_gate_source():
    task_id = {"plan_id": "PLAN-001", "phase_id": "PH-1", "task_id": "GATE"}
    schema = load("task.schema.json")
    # validate just the source subobject through a full task
    task = {
        "schema_version": "0.2.0",
        "id": "PLAN-001/PH-1/GATE",
        "tier": "fable",
        "status": "paused_for_human",
        "source": task_id,
        "goal": "Human review gate",
        "context": {"repo_state": "HEAD", "chain_depth": 0, "depends_on": []},
        "done": {"statement": "phase PR merged"},
        "attempts": 0,
        "created_at": "2026-06-12T00:00:00+00:00",
        "created_by": "sb",
    }
    schema.validate(task)


def test_decision_schema_accepts_steelman_and_blast_radius():
    rec = {
        "schema_version": "0.3.0",
        "id": "ADR-051",
        "type": "agent",
        "status": "proposed",
        "timestamp": "2026-06-12T00:00:00+00:00",
        "title": "Use immutable snapshots",
        "author": {"kind": "model", "id": "claude-opus-4-8"},
        "steelman": [
            {"option": "mutable-with-locks", "strongest_case": "Lower memory; familiar pattern."}
        ],
        "blast_radius": "Cache module only; no API surface change.",
    }
    load("decision-record.schema.json").validate(rec)
