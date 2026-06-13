import os

import pytest

from sb import claims, results, store
from sb.paths import DEFAULT_CONFIG
from tests.helpers import make_task


def active_task(lay, **over):
    t = make_task(**over)
    store.write_task(lay, "queued", t)
    return claims.claim_one(lay, "w1")


def write_result(lay, task_id, **fields):
    r = {"schema_version": "0.1.0", "outcome": "success", "summary": "done", **fields}
    store.write_json(os.path.join(lay.results, store.fname(task_id)), r)


def test_success_awaits_verification_and_enqueues_verify_task(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "paused"
    lane, on_disk = store.find_task(lay, t["id"])
    assert lane == "paused" and on_disk["status"] == "awaiting_verification"
    assert on_disk["result"]["outcome"] == "success"

    vlane, vtask = store.find_task(lay, f"{t['id']}.V1")
    assert vlane == "queued"
    assert vtask["context"]["verifies"] == t["id"]
    assert vtask["tier"] == "sonnet"  # author opus -> configured verifier


def test_verifier_tier_falls_back_when_author_matches(lay):
    t = active_task(lay, tier="sonnet")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    _, vtask = store.find_task(lay, f"{t['id']}.V1")
    assert vtask["tier"] == "opus"


def test_verification_tasks_are_not_reverified(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2", tier="sonnet")
    write_result(lay, v["id"], verdict="pass")
    dest = results.file_result(lay, DEFAULT_CONFIG, v["id"])
    assert dest == "done"
    assert store.find_task(lay, f"{v['id']}.V1") == (None, None)


def test_verdict_pass_moves_target_done(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2", tier="sonnet")
    write_result(lay, v["id"], verdict="pass")
    results.file_result(lay, DEFAULT_CONFIG, v["id"])
    lane, target = store.find_task(lay, t["id"])
    assert lane == "done" and target["status"] == "done"


def test_verdict_fail_requeues_target_with_notes(lay):
    t = active_task(lay, tier="opus")
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2", tier="sonnet")
    write_result(lay, v["id"], verdict="fail", verdict_notes="stress test fails")
    results.file_result(lay, DEFAULT_CONFIG, v["id"])
    lane, target = store.find_task(lay, t["id"])
    assert lane == "queued" and target["attempts"] == 1
    prior = target["context"]["prior_attempts"][0]
    assert prior["verifier_notes"] == "stress test fails"
    assert "result" not in target


def test_verdict_fail_at_max_attempts_fails_target(lay):
    t = active_task(lay)
    _, on_disk = store.find_task(lay, t["id"])
    on_disk["attempts"] = 2  # one more failure hits max_attempts=3
    store.write_task(lay, "active", on_disk)
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    v = claims.claim_one(lay, "w2")
    write_result(lay, v["id"], verdict="fail", verdict_notes="still broken")
    results.file_result(lay, DEFAULT_CONFIG, v["id"])
    lane, target = store.find_task(lay, t["id"])
    assert lane == "failed" and target["status"] == "failed"


def test_blocked_pauses_for_human(lay):
    t = active_task(lay)
    write_result(lay, t["id"], outcome="blocked", summary="missing credential")
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "paused"
    assert store.find_task(lay, t["id"])[1]["status"] == "paused_for_human"


def test_partial_requeues_and_increments_attempts(lay):
    t = active_task(lay)
    write_result(lay, t["id"], outcome="partial", summary="half done")
    dest = results.file_result(lay, DEFAULT_CONFIG, t["id"])
    assert dest == "queued"
    _, on_disk = store.find_task(lay, t["id"])
    assert on_disk["attempts"] == 1
    assert on_disk["context"]["prior_attempts"][0]["summary"] == "half done"


def test_double_filing_explains_itself(lay):
    t = active_task(lay)
    write_result(lay, t["id"])
    results.file_result(lay, DEFAULT_CONFIG, t["id"])
    with pytest.raises(FileNotFoundError, match="already filed"):
        results.file_result(lay, DEFAULT_CONFIG, t["id"])


def test_missing_result_file_raises(lay):
    t = active_task(lay)
    with pytest.raises(FileNotFoundError):
        results.file_result(lay, DEFAULT_CONFIG, t["id"])


def test_invalid_result_rejected(lay):
    t = active_task(lay)
    store.write_json(os.path.join(lay.results, store.fname(t["id"])),
                     {"schema_version": "0.1.0", "outcome": "success"})
    with pytest.raises(ValueError):
        results.file_result(lay, DEFAULT_CONFIG, t["id"])
