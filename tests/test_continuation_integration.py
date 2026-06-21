"""Stub-dispatcher integration test for the research-handoff continuation chain
(§3.3, ADR-005/006). No model: result files are canned. Asserts the parent pauses
for research, the research task runs+verifies to done, the parent re-claims as a
continuation, and its research findings are fetchable."""
import os

from sb import claims, paths, results, store
from tests.helpers import make_task


def write_result(lay, task_id, **fields):
    r = {"schema_version": "0.2.0", "outcome": "success", "summary": "ok", **fields}
    store.write_json(os.path.join(lay.results, store.fname(task_id)), r)


def test_full_continuation_chain(lay):
    cfg = paths.load_config(lay)
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1", tier="opus"))

    # 1. claim parent, it hits a research handoff
    parent = claims.claim_one(lay, "w1", cfg=cfg)
    write_result(lay, parent["id"], outcome="paused_for_research",
                 summary="partial: scaffolded, need a benchmark",
                 research={"goal": "benchmark designs", "tier": "haiku",
                           "done_statement": "comparison table exists"})
    assert results.file_result(lay, cfg, parent["id"]) == "queued"
    rid = "PLAN-001/PH-1/T-1.R1"
    assert store.find_task(lay, rid)[0] == "queued"

    # 2. parent is NOT claimable yet (depends on the research task)
    got = claims.claim_one(lay, "w1", cfg=cfg)
    assert got["id"] == rid                       # only the research task is claimable

    # 3. research succeeds -> verify -> verdict pass -> research done
    write_result(lay, rid, outcome="success", summary="benchmark: snapshot wins p99")
    assert results.file_result(lay, cfg, rid) == "paused"
    v = claims.claim_one(lay, "w1", cfg=cfg)
    assert v["id"] == f"{rid}.V1"
    write_result(lay, v["id"], verdict="pass", verdict_notes="benchmark is sound")
    assert results.file_result(lay, cfg, v["id"]) == "done"
    assert store.find_task(lay, rid)[1]["status"] == "done"

    # 4. parent now re-claimable as a continuation; research finding is fetchable
    cont = claims.claim_one(lay, "w1", cfg=cfg)
    assert cont["id"] == parent["id"]
    assert cont["context"]["prior_attempts"][0]["summary"].startswith("partial:")
    finding = results.read_result(lay, rid)
    assert finding["summary"] == "benchmark: snapshot wins p99"
