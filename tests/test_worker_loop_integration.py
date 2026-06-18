"""Stub-dispatcher integration test for the /sb-work loop (spec §6).

Stands in for the model: a 'dispatch' just writes a canned result file. Uses the
real sb engine and real `git worktree` to assert the choreography the skill
relies on — claim, worktree create, file-result lane move, worktree remove,
then the verify pass promoting the target to done.
"""
import json
import os
import subprocess

import pytest

from sb import claims, paths, results, store
from tests.helpers import make_task


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def repo(tmp_path):
    r = str(tmp_path)
    git(r, "init", "-q")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("seed\n")
    git(r, "add", "README.md")
    git(r, "commit", "-qm", "init")
    return r


def write_result(lay, task_id, **over):
    res = {"schema_version": "0.1.0", "outcome": "success",
           "summary": "did the thing"}
    res.update(over)
    store.write_json(os.path.join(lay.results, store.fname(task_id)), res)


def test_loop_choreography_and_worktree_lifecycle(repo):
    lay = paths.init(repo)
    cfg = paths.load_config(lay)
    task = make_task("PLAN-001/PH-1/T-1", tier="haiku",
                     context={"branch": "sb/plan-001/ph-1", "depends_on": []},
                     done={"statement": "thing done",
                           "verify": {"kind": "command", "ref": "true"}})
    store.write_task(lay, "queued", task)

    # 1. claim
    claimed = claims.claim_one(lay, "w1", cfg=cfg)
    assert claimed["id"] == "PLAN-001/PH-1/T-1"
    assert store.find_task(lay, claimed["id"])[0] == "active"

    # 2. provision worktree off the phase branch (skill's job; done here w/ real git)
    branch = claimed["context"]["branch"]
    wt = os.path.join(repo, ".worktrees", "w1")
    git(repo, "worktree", "add", "-q", "-b", branch, wt, "HEAD")
    assert os.path.isdir(wt)

    # 3. stub dispatch: subagent would commit work + write the result file
    (open(os.path.join(wt, "f.txt"), "w")).write("work\n")
    git(wt, "add", "f.txt")
    git(wt, "commit", "-qm", "task work")
    write_result(lay, claimed["id"])

    # 4. file-result: success -> paused (awaiting verification) + verify enqueued
    dest = results.file_result(lay, cfg, claimed["id"])
    assert dest == "paused"
    lane, t = store.find_task(lay, claimed["id"])
    assert lane == "paused" and t["status"] == "awaiting_verification"
    verify_id = "PLAN-001/PH-1/T-1.V1"
    vlane, vtask = store.find_task(lay, verify_id)
    assert vlane == "queued" and vtask["context"]["verifies"] == claimed["id"]

    # 5. teardown: branch + commit persist after worktree removal
    git(repo, "worktree", "remove", "--force", wt)
    assert not os.path.isdir(wt)
    log = subprocess.run(["git", "log", "--oneline", branch], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    assert "task work" in log

    # 6. verify pass promotes the target to done
    vclaimed = claims.claim_one(lay, "w1", cfg=cfg)
    assert vclaimed["id"] == verify_id
    write_result(lay, verify_id, verdict="pass", verdict_notes="looks right")
    vdest = results.file_result(lay, cfg, verify_id)
    assert vdest == "done"
    target_lane, target = store.find_task(lay, claimed["id"])
    assert target_lane == "done" and target["status"] == "done"


def test_loop_releases_on_simulated_rate_limit(repo):
    """A dispatch that 'raises a rate-limit' before producing a result -> the
    loop calls release; the task is claimable again with attempts unchanged."""
    lay = paths.init(repo)
    cfg = paths.load_config(lay)
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    claimed = claims.claim_one(lay, "w1", cfg=cfg)
    # simulate: dispatch raised before writing any result -> release
    dest = claims.release(lay, claimed["id"])
    assert dest == "queued"
    again = claims.claim_one(lay, "w2", cfg=cfg)
    assert again["id"] == "PLAN-001/PH-1/T-1"
    assert again["attempts"] == 0
