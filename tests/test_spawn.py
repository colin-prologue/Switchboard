import os

import pytest

from sb import claims, leases, spawn, store
from sb.paths import DEFAULT_CONFIG
from tests.helpers import make_task


def claimed_parent(lay, **over):
    t = make_task(**over)
    store.write_task(lay, "queued", t)
    return claims.claim_one(lay, "w1")


def test_spawn_creates_research_and_requeues_parent(lay):
    parent = claimed_parent(lay)
    research = spawn.spawn_research(
        lay, DEFAULT_CONFIG, parent["id"],
        goal="Benchmark both cache designs", tier="haiku",
        done_statement="A benchmark table exists comparing the designs.")
    assert research["id"] == f"{parent['id']}.R1"
    assert research["tier"] == "haiku"
    assert research["context"]["chain_depth"] == 1
    assert store.find_task(lay, research["id"])[0] == "queued"

    lane, p = store.find_task(lay, parent["id"])
    assert lane == "queued" and p["status"] == "queued"
    assert research["id"] in p["context"]["depends_on"]
    assert "claim" not in p
    assert leases.read_lease(lay, parent["id"]) is None


def test_spawn_carries_partial_result(lay):
    parent = claimed_parent(lay)
    rpath = os.path.join(lay.results, store.fname(parent["id"]))
    store.write_json(rpath, {"schema_version": "0.2.0", "outcome": "blocked",
                             "summary": "Need benchmark data before deciding."})
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g", tier="haiku", done_statement="d")
    _, p = store.find_task(lay, parent["id"])
    assert p["context"]["prior_attempts"][0]["summary"].startswith("Need benchmark")
    assert not os.path.exists(rpath)  # consumed


def test_spawn_discards_corrupt_partial(lay):
    parent = claimed_parent(lay)
    rpath = os.path.join(lay.results, store.fname(parent["id"]))
    with open(rpath, "w", encoding="utf-8") as f:
        f.write("{torn")
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g", tier="haiku", done_statement="d")
    _, p = store.find_task(lay, parent["id"])
    assert "corrupt" in p["context"]["prior_attempts"][0]["note"]
    assert not os.path.exists(rpath)


def test_spawn_suffix_increments(lay):
    parent = claimed_parent(lay)
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g1", tier="haiku", done_statement="d")
    # parent went back to queued; clear its dep so it can be claimed again
    _, p = store.find_task(lay, parent["id"])
    p["context"]["depends_on"] = []
    store.write_task(lay, "queued", p)
    claims.claim_one(lay, "w1", tier="haiku")  # claims R1 (sorts first)
    claims.claim_one(lay, "w1", tier="haiku")  # claims the parent
    r2 = spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                              goal="g2", tier="haiku", done_statement="d")
    assert r2["id"].endswith(".R2")


def test_spawn_rejects_queued_continuation_parent(lay):
    parent = claimed_parent(lay)
    spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                         goal="g1", tier="haiku", done_statement="d")
    # parent is queued as a continuation; spawning from it must fail —
    # only the active claimer owns the right to spawn
    with pytest.raises(ValueError, match="not active"):
        spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                             goal="g2", tier="haiku", done_statement="d")


def test_spawn_depth_cap_pauses_for_human(lay):
    parent = claimed_parent(lay, context={"chain_depth": 3})
    out = spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                               goal="g", tier="haiku", done_statement="d")
    assert out is None
    lane, p = store.find_task(lay, parent["id"])
    assert lane == "paused" and p["status"] == "paused_for_human"
    assert "chain depth" in p["failure"]["reason"]


def test_spawn_depth_cap_consumes_partial(lay):
    # Codex C1 (PR #3): a handoff filed AT the chain-depth cap must still preserve
    # the partial (so the human sees why it stalled) and not orphan the result
    # file under .switchboard/results/.
    parent = claimed_parent(lay, context={"chain_depth": 3})
    rpath = os.path.join(lay.results, store.fname(parent["id"]))
    store.write_json(rpath, {"schema_version": "0.2.0",
                             "outcome": "paused_for_research",
                             "summary": "scaffolded; needed a benchmark"})
    out = spawn.spawn_research(lay, DEFAULT_CONFIG, parent["id"],
                               goal="g", tier="haiku", done_statement="d")
    assert out is None
    _, p = store.find_task(lay, parent["id"])
    assert p["context"]["prior_attempts"][0]["summary"] == "scaffolded; needed a benchmark"
    assert not os.path.exists(rpath)  # consumed, not orphaned


def test_spawn_requires_active_parent(lay):
    t = make_task()
    store.write_task(lay, "queued", t)
    with pytest.raises(ValueError, match="not active"):
        spawn.spawn_research(lay, DEFAULT_CONFIG, t["id"],
                             goal="g", tier="haiku", done_statement="d")
