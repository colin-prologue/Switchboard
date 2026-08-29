"""Tests for the shared status-transition table (issue #29, part A).

Covers the acceptance criteria that pin the table to ONE committed file:
- the orchestrator loads `requires_marker` from a single committed path constant
- no transition-table literal is duplicated in Python
- phase-2 (`fail-review`) edges are annotated inactive; the active cap-hit edge
  targets `parked`; the degraded `todo -> human-review` edge carries its note
"""

from __future__ import annotations

from pathlib import Path

import yaml

from orchestrator.transitions import TRANSITIONS_PATH, load_requires_marker

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ORCH_SRC = _REPO_ROOT / "orchestrator" / "src" / "orchestrator"


def _raw_table() -> dict:
    return yaml.safe_load(TRANSITIONS_PATH.read_text(encoding="utf-8"))


# --- single committed path constant -------------------------------------------

def test_path_constant_points_at_committed_file():
    assert TRANSITIONS_PATH == _REPO_ROOT / "workflow" / "transitions.yml"
    assert TRANSITIONS_PATH.is_file()


def test_requires_marker_loaded_from_yaml_not_python_literal():
    loaded = load_requires_marker()
    # todo is the only gated state; the marker is gate:triage-passed.
    assert loaded == {"todo": ["gate:triage-passed"]}
    # And it genuinely came from the file, not a Python default: the loaded
    # mapping matches the YAML's own requires_marker section verbatim.
    section = _raw_table()["requires_marker"]
    assert section == {"todo": ["gate:triage-passed"]}


def test_no_transition_table_literal_in_python():
    """The marker string (and thus the table) must live in YAML only — no
    duplicated literal in orchestrator Python (the drift the AC guards)."""
    offenders = []
    for py in _ORCH_SRC.rglob("*.py"):
        if "gate:triage-passed" in py.read_text(encoding="utf-8"):
            offenders.append(py.relative_to(_REPO_ROOT))
    assert offenders == [], f"table literal duplicated in Python: {offenders}"


# --- phasing (verdict 2026-07-06 finding 3) -----------------------------------

def _edges() -> list[dict]:
    return _raw_table()["edges"]


def test_active_cap_hit_edges_are_the_five_issue_31_routes():
    """Pre-#31 there was exactly one active cap-hit edge (in-progress -> parked).
    #31 added the fail-review entry edges and the two park edges the fallback and
    the verify-role cap-out actually produce, so the pin becomes a set."""
    caphit = [e for e in _edges() if e.get("trigger") == "cap-hit"]
    active = {(e["from"], e["to"]) for e in caphit if e.get("active", True)}
    assert active == {
        # implement cap-hit -> diagnosis (todo is the DOMINANT entry)
        ("todo", "fail-review"),
        ("in-progress", "fail-review"),
        # implement cap-hit -> park: unprovisioned-label fallback, episode cap
        ("todo", "parked"),
        ("in-progress", "parked"),
        # verify-role cap-out: unchanged by #31
        ("triage", "parked"),
        # the fail-review session itself capping out
        ("fail-review", "parked"),
    }


def test_fail_review_edges_are_active_and_ungated():
    """#29 pre-encoded these as `active: false` / `requires: "#20b"`; #31 shipped
    the verifier, so nothing may still be gated on the parent ticket."""
    fail_edges = [e for e in _edges()
                  if e["from"] == "fail-review" or e["to"] == "fail-review"]
    assert fail_edges, "expected fail-review edges to be present"
    for e in fail_edges:
        assert e.get("active", True) is True, f"fail-review edge inactive: {e}"
        assert "requires" not in e, f"fail-review edge still gated: {e}"


# --- NEEDS DECISION: the two decision edges (issue #55) -----------------------

def test_triage_to_decision_edge_matches_needs_decision_verdict():
    edges = [e for e in _edges() if e["from"] == "triage" and e["to"] == "decision"]
    assert len(edges) == 1, f"expected exactly one triage -> decision edge, got {edges}"
    assert edges[0] == {
        "from": "triage",
        "to": "decision",
        "actor": "triage-verifier",
        "verdict": "needs-decision",
    }


def test_decision_to_drafting_edge_is_the_human_fold_path():
    edges = [e for e in _edges() if e["from"] == "decision"]
    assert len(edges) == 1, f"decision must have exactly one edge out, got {edges}"
    edge = edges[0]
    assert edge["to"] == "drafting"
    assert edge["actor"] == "human"
    assert edge["verdict"] == "answered"
    assert edge["remove_marker"] == "gate:triage-passed"
    assert "#51" in edge["note"]


def test_decision_to_triage_is_illegal():
    """The answer must be folded into the body first; the fold path is the
    existing drafting -> triage edge, not a shortcut back into triage."""
    assert not [e for e in _edges() if e["from"] == "decision" and e["to"] == "triage"]


def test_decision_adds_exactly_two_edges_and_no_requires_marker():
    touching = [e for e in _edges() if "decision" in (e["from"], e["to"])]
    assert len(touching) == 2, f"expected exactly two decision edges, got {touching}"
    # requires_marker keys only `todo` — a gate state never gates on a marker.
    assert "decision" not in _raw_table()["requires_marker"]


# --- review-response re-entry: the widened human-review -> todo edge (#43) ----

def test_human_review_to_todo_edge_carries_both_actors_and_the_trigger():
    """One edge, two actors (issue #43 / AgDR-037).

    The review-response sub-poll deliberately reuses the EXISTING re-entry edge
    rather than minting a `status:review-response` state, so this table is the
    only place the widening is recorded. Exact equality per convention: an edge
    that quietly lost `orchestrator` or the trigger key would leave the
    orchestrator taking an undocumented transition.
    """
    edges = [e for e in _edges()
             if e["from"] == "human-review" and e["to"] == "todo"]
    assert len(edges) == 1, f"expected exactly one re-entry edge, got {edges}"
    edge = edges[0]
    assert edge["actor"] == ["human", "orchestrator"]
    assert edge["verdict"] == "changes-requested"  # the human path is unchanged
    assert edge["trigger"] == "review-response"
    assert set(edge) == {"from", "to", "actor", "verdict", "trigger", "note"}


def test_both_actors_on_the_re_entry_edge_reset_the_implement_budget():
    """Issue #178: the edge's TWO actors must agree on the budget, not just on
    the label they write.

    The pre-#178 note recorded a reset on the orchestrator path only, and that
    asymmetry is the defect — a human revision request on a ticket with a spent
    implement budget opened a fail-review episode or parked instead of
    re-dispatching. The table is where the widening was recorded, so it is
    where the correction has to land too; a note that still says only one actor
    resets would leave the next reader with the belief the code no longer has.

    This is a documentation pin, not the behaviour check — the behaviour lives
    in `test_review_response.py` (grant, no park, cap binds) and
    `test_fail_review.py` (no consumed episode).
    """
    edge = [e for e in _edges()
            if e["from"] == "human-review" and e["to"] == "todo"][0]
    note = edge["note"]
    assert "#178" in note
    assert "BOTH actors reset" in note
    # The bound is SHARED, which is the part an actor-scoped reading would get
    # wrong: two actors with a round budget each is 2x the allowance the cap
    # was chosen to permit.
    assert "ONE round budget serves both actors" in note


def test_review_response_adds_no_new_state_and_no_requires_marker():
    """The decision's load-bearing claim: no new state, no new gate.

    `requires_marker` keys `todo` on `gate:triage-passed`, and that marker
    deliberately survives the re-entry — so the relabeled todo is claimable
    without the sub-poll writing any marker of its own.
    """
    states = {e["from"] for e in _edges()} | {e["to"] for e in _edges()}
    assert "review-response" not in states
    assert set(_raw_table()["requires_marker"]) == {"todo"}

# --- the fold edge (issue #126 part b) ---------------------------------------

def test_drafting_to_triage_has_exactly_two_edges_human_and_fold():
    """Apply relabels drafting -> triage, so the table must record a second
    actor on that edge. The human edge is KEPT — a hand fold still exists."""
    edges = [e for e in _edges() if e["from"] == "drafting" and e["to"] == "triage"]
    assert len(edges) == 2, f"expected exactly two drafting -> triage edges, got {edges}"
    assert {e["actor"] for e in edges} == {"human", "fold"}


def test_degraded_todo_to_human_review_edge_annotated():
    degraded = [e for e in _edges() if e.get("degraded")]
    match = [e for e in degraded if e["from"] == "todo" and e["to"] == "human-review"]
    assert match, "degraded todo -> human-review edge missing its annotation"
