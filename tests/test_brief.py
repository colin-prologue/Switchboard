from sb import brief, store
from tests.helpers import make_agdr, make_task, put_decision

PLAN = {
    "schema_version": "0.1.0", "plan_id": "PLAN-001", "goal": "toy goal",
    "created": "2026-06-14T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "intent": "Decide the model before code.",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [{"task_id": "T-1", "title": "Choose the design",
                    "done": {"statement": "ADR exists"}}]},
    ],
}


def done_task(lay, tid, **over):
    t = make_task(tid, status="done", **over)
    t["result"] = {"schema_version": "0.1.0", "outcome": "success",
                   "summary": "Implemented and tested.",
                   "evidence": [{"kind": "commit", "ref": "abc123"}]}
    store.write_task(lay, "done", t)
    return t


def passed_verify(lay, target_id, notes="looks correct"):
    v = make_task(f"{target_id}.V1", status="done",
                  context={"verifies": target_id})
    v["result"] = {"schema_version": "0.1.0", "outcome": "success",
                   "summary": "verified", "verdict": "pass", "verdict_notes": notes}
    store.write_task(lay, "done", v)


def test_brief_has_header_and_goal(lay):
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "# Review: PLAN-001 / PH-1 — Design" in md
    assert "toy goal" in md
    assert "Decide the model before code." in md


def test_brief_shows_rich_agdr_profile(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "Use immutable snapshots for the cache" in md
    assert "immutable-snapshots" in md            # chosen
    assert "mutable-with-locks" in md             # alternative + steelman
    assert "Lower memory; a familiar pattern." in md   # steelman case
    assert "Cache module only" in md              # blast radius
    assert "medium" in md                         # confidence


def test_brief_surfaces_pending_agdrs_up_top(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    md = brief.build_brief(lay, PLAN, "PH-1")
    attention = md.index("Needs your attention")
    decisions = md.index("Decisions made")
    assert attention < decisions   # HDR-010: pending review surfaced first
    assert "ADR-051" in md[attention:decisions]


def test_brief_shows_work_with_verdict(lay):
    done_task(lay, "PLAN-001/PH-1/T-1")
    passed_verify(lay, "PLAN-001/PH-1/T-1", notes="stress test green")
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "do the thing" in md                 # goal of make_task
    assert "Implemented and tested." in md
    assert "verified: pass" in md
    assert "stress test green" in md


def test_brief_excludes_gate_and_verify_tasks_from_work(lay):
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human")
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)
    passed_verify(lay, "PLAN-001/PH-1/T-1")
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "GATE" not in md.split("Work delivered")[1]


def test_brief_flags_failed_task(lay):
    t = make_task("PLAN-001/PH-1/T-1", status="failed")
    t["failure"] = {"reason": "verification failed: race remains"}
    store.write_task(lay, "failed", t)
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "Needs your attention" in md
    assert "race remains" in md
