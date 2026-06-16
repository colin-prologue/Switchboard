import json
import os

from sb import cli, store
from sb.paths import Layout
from tests.helpers import make_agdr

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


def run_json(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, (json.loads(out) if out else None)


def setup_plan(repo):
    cli.main(["init", "--repo", repo])
    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(PLAN, f)
    cli.main(["seed", "--repo", repo, "--plan", plan_path])


def test_status_emit_persists_digest(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    capsys.readouterr()
    code, dg = run_json(capsys, "status", "--repo", repo, "--emit")
    assert code == 0 and dg["schema_version"] == "0.1.0"
    assert os.path.exists(os.path.join(repo, ".switchboard", "digest.json"))


def test_brief_prints_markdown(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    lay = Layout(repo)
    with open(os.path.join(lay.decisions, "ADR-051.json"), "w",
              encoding="utf-8") as f:
        json.dump(make_agdr("ADR-051", status="pending-review"), f)
    capsys.readouterr()
    code = cli.main(["brief", "--repo", repo, "--plan", "PLAN-001",
                     "--phase", "PH-1"])
    md = capsys.readouterr().out
    assert code == 0
    assert "# Review: PLAN-001 / PH-1" in md
    assert "ADR-051" in md


def test_stamp_approve_unblocks_next_phase_via_cli(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    lay = Layout(repo)
    # drive PH-1/T-1 to done
    store.move_task(lay, "queued", "done", "PLAN-001/PH-1/T-1")
    _, t = store.find_task(lay, "PLAN-001/PH-1/T-1")
    t["status"] = "done"
    store.write_task(lay, "done", t)
    capsys.readouterr()

    code, out = run_json(capsys, "stamp", "--repo", repo, "--plan", "PLAN-001",
                         "--phase", "PH-1", "--action", "approve", "--note", "ok")
    assert code == 0 and out["gate_advanced"] is True

    code, task = run_json(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    assert code == 0 and task["id"] == "PLAN-001/PH-2/T-2"


def test_stamp_not_ready_exits_2(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)              # T-1 still queued
    capsys.readouterr()
    code = cli.main(["stamp", "--repo", repo, "--plan", "PLAN-001",
                     "--phase", "PH-1", "--action", "approve", "--note", "x"])
    assert code == 2


def test_notify_fires_then_quiet_via_cli(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    lay = Layout(repo)
    with open(os.path.join(lay.decisions, "ADR-051.json"), "w",
              encoding="utf-8") as f:
        json.dump(make_agdr("ADR-051", status="pending-review"), f)
    capsys.readouterr()
    code, out = run_json(capsys, "notify", "--repo", repo, "--channel", "null")
    assert code == 0 and "pending_agdr:ADR-051" in out["fired"]
    code, out2 = run_json(capsys, "notify", "--repo", repo, "--channel", "null")
    assert out2["fired"] == []
