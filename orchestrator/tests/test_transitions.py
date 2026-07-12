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


def test_active_cap_hit_edge_targets_parked():
    caphit = [e for e in _edges() if e.get("trigger") == "cap-hit"]
    active = [e for e in caphit if e.get("active", True)]
    assert active, "expected an active cap-hit edge"
    assert all(e["from"] == "in-progress" and e["to"] == "parked" for e in active)


def test_fail_review_edges_are_inactive_and_gated_on_20b():
    fail_edges = [e for e in _edges()
                  if e["from"] == "fail-review" or e["to"] == "fail-review"]
    assert fail_edges, "expected fail-review edges to be present (annotated)"
    for e in fail_edges:
        assert e.get("active", True) is False, f"fail-review edge active: {e}"
        assert e.get("requires") == "#20b", f"fail-review edge not gated on #20b: {e}"


def test_degraded_todo_to_human_review_edge_annotated():
    degraded = [e for e in _edges() if e.get("degraded")]
    match = [e for e in degraded if e["from"] == "todo" and e["to"] == "human-review"]
    assert match, "degraded todo -> human-review edge missing its annotation"
