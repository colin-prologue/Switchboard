import json
import os

import pytest

from sb import cli, store
from sb.paths import Layout
from tests.test_seed import PLAN


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, json.loads(out) if out else None


def write_result(lay, task_id, **fields):
    r = {"schema_version": "0.2.0", "outcome": "success", "summary": "ok",
         **fields}
    store.write_json(os.path.join(lay.results, store.fname(task_id)), r)


def test_full_pipeline_through_cli(tmp_path, capsys):
    repo = str(tmp_path)
    lay = Layout(repo)

    assert cli.main(["init", "--repo", repo]) == 0
    capsys.readouterr()

    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(PLAN, f)
    code, seeded = run(capsys, "seed", "--repo", repo, "--plan", plan_path)
    assert code == 0 and len(seeded["seeded"]) == 2

    # claim PH-1's task, file success, verify it, pass the verdict
    code, task = run(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    assert code == 0 and task["id"] == "PLAN-001/PH-1/T-1"

    write_result(lay, task["id"])
    code, out = run(capsys, "file-result", task["id"], "--repo", repo)
    assert code == 0 and out["lane"] == "paused"

    code, vtask = run(capsys, "claim", "--repo", repo, "--worker-id", "w2")
    assert vtask["context"]["verifies"] == task["id"]
    write_result(lay, vtask["id"], verdict="pass")
    run(capsys, "file-result", vtask["id"], "--repo", repo)
    assert store.find_task(lay, task["id"])[0] == "done"

    # PH-2 stays blocked behind the un-stamped PH-1 gate
    code, nothing = run(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    assert code == 3 and nothing is None


def test_claim_exit_code_when_empty(tmp_path, capsys):
    repo = str(tmp_path)
    cli.main(["init", "--repo", repo])
    capsys.readouterr()
    assert cli.main(["claim", "--repo", repo, "--worker-id", "w1"]) == 3


def test_spawn_via_cli(tmp_path, capsys):
    repo = str(tmp_path)
    lay = Layout(repo)
    cli.main(["init", "--repo", repo])
    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(PLAN, f)
    cli.main(["seed", "--repo", repo, "--plan", plan_path])
    capsys.readouterr()
    code, task = run(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    code, research = run(capsys, "spawn", "--repo", repo, "--task", task["id"],
                         "--goal", "research it", "--tier", "haiku",
                         "--done", "research summary exists")
    assert code == 0 and research["id"] == f"{task['id']}.R1"


def test_seed_blocked_questions_exit_code(tmp_path, capsys):
    repo = str(tmp_path)
    cli.main(["init", "--repo", repo])
    plan = dict(PLAN, open_questions=[{"question": "SLA?", "blocking": True,
                                       "resolve_by": "human"}])
    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f)
    capsys.readouterr()
    assert cli.main(["seed", "--repo", repo, "--plan", plan_path]) == 2
