"""Explicit state precedence for multi-label issues (issue #167, part a).

`status:parked` is an OVERLAY — "hold this wherever it is" — applied alongside
the stage label a ticket resumes into, so an issue carrying two `status:*`
labels is a designed steady state and the winner has to be a stated decision.
It used to be `sorted(...)[0]`, which put `status:parked` behind
`status:fail-review` and `status:in-progress` by alphabetical accident: a parked
issue derived an ACTIVE state, and three separate comments in the tree promised
to keep the coincidence true.

These tests pin the replacement: one committed precedence list in
`workflow/transitions.yml`, consulted by `tracker.normalize_status_state`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from orchestrator.tracker import normalize_status_state, status_precedence
from orchestrator.transitions import TRANSITIONS_PATH, load_precedence
from orchestrator.types import WorkflowError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REGISTER_SCRIPT = _REPO_ROOT / "scripts" / "register-project.sh"

# The full committed order, pinned verbatim and highest-priority first. A
# transitions.yml edit that reorders or drops a state fails HERE, deliberately:
# the point of #167 is that this order is a decision somebody made, not a
# property of the alphabet, so changing it must be a visible act.
EXPECTED_PRECEDENCE = (
    "parked",
    "human review",
    "plan review",
    "review",
    "decision",
    "drafting",
    "blocked",
    "fail review",
    "triage",
    "in progress",
    "todo",
)


def _raw_precedence() -> list[str]:
    return yaml.safe_load(TRANSITIONS_PATH.read_text(encoding="utf-8"))["precedence"]


# --- the list itself ----------------------------------------------------------

def test_precedence_comes_from_the_committed_yaml_not_a_python_literal():
    loaded = load_precedence()
    assert loaded == EXPECTED_PRECEDENCE
    # ...and it genuinely came from the file: the YAML's own rows, in order,
    # rewritten only from the dashed spelling to the tracker's spaced one.
    assert [s.replace("-", " ") for s in _raw_precedence()] == list(loaded)


def test_tracker_default_precedence_is_the_committed_list():
    """`normalize_status_state`'s default is the file, not a hard-coded order."""
    assert status_precedence() == load_precedence()


def test_precedence_ranks_parked_above_every_park_reachable_state():
    """The rank the section exists for, asserted as a rank and not as an outcome.

    The park-reachable states are every `to: parked` edge's source in the
    transitions table — derived, so a new park entry edge lands here rather than
    quietly inheriting whatever the alphabet says.
    """
    edges = yaml.safe_load(TRANSITIONS_PATH.read_text(encoding="utf-8"))["edges"]
    reachable = {
        str(e["from"]).replace("-", " ") for e in edges if e.get("to") == "parked"
    }
    assert reachable, "expected transitions.yml to declare park entry edges"

    order = load_precedence()
    assert order[0] == "parked"
    for state in reachable:
        assert state in order, f"park-reachable state {state!r} is unranked"
        assert order.index("parked") < order.index(state)


def test_every_provisioned_status_label_is_ranked():
    """No `status:*` label a project is given may fall off the list.

    An unranked label still derives deterministically (it sorts last,
    alphabetically among its peers) but it would lose to EVERY real state,
    including `status:todo` — which is exactly the silent mis-derivation this
    section replaces. Provisioned labels are read from the registration script
    so adding one there without ranking it fails here.
    """
    provisioned = set(
        re.findall(r'mklabel\s+"status:([a-z-]+)"', _REGISTER_SCRIPT.read_text())
    )
    assert provisioned, "expected register-project.sh to provision status labels"
    unranked = {s.replace("-", " ") for s in provisioned} - set(load_precedence())
    assert unranked == set(), f"provisioned but unranked: {sorted(unranked)}"


# --- derivation: the pairs that were sort-order-dependent ---------------------

@pytest.mark.parametrize(
    "labels",
    [
        ["status:in-progress", "status:parked"],
        ["status:fail-review", "status:parked"],
    ],
)
def test_parked_beats_the_orchestrator_claim_labels(labels):
    """Both pairs derived an ACTIVE state under alphabetical order.

    `status:in-progress` and `status:fail-review` both sort before
    `status:parked`, so a park whose best-effort strip failed (or a poll landing
    between the park write and the strip) reported a held ticket as live, and
    only the separate PARK_LABEL check kept it out of dispatch.
    """
    assert sorted(labels)[0] != "status:parked"  # the old rule's answer
    assert normalize_status_state(labels, closed=False) == "parked"


@pytest.mark.parametrize("other", ["status:todo", "status:triage"])
def test_parked_still_beats_the_labels_park_deliberately_keeps(other):
    """The pairs alphabet already got right stay right — for a stated reason now.

    These two are the resume targets `_park` deliberately leaves in place, so
    they are the steady-state dual encoding, not a transient.
    """
    assert normalize_status_state([other, "status:parked"], closed=False) == "parked"


def test_gate_state_beats_a_residual_claim_label():
    """The AC pair. `human-review` wins because a handoff already happened.

    Alphabet agrees here, which is precisely why it needs pinning: nothing about
    today's output tells you whether the rule is load-bearing. Reordering the
    list flips the answer, so the list is what decides.
    """
    labels = ["status:in-progress", "status:human-review"]
    assert normalize_status_state(labels, closed=False) == "human review"

    reordered = ("in progress",) + tuple(
        s for s in EXPECTED_PRECEDENCE if s != "in progress"
    )
    assert normalize_status_state(
        labels, closed=False, precedence=reordered
    ) == "in progress"


def test_only_status_namespace_labels_participate():
    """Derivation is pure over the `status:` namespace.

    `hold:parked` need not exist anywhere — the point is that a marker OUTSIDE
    the namespace cannot become the state, which is the invariant part (b)'s
    migration will land on.
    """
    assert normalize_status_state(
        ["status:todo", "hold:parked"], closed=False
    ) == "todo"
    assert normalize_status_state(
        ["gate:triage-passed", "gate:fail-reviewed"], closed=False
    ) == "none"


# --- derivation: everything else is unchanged --------------------------------

def test_single_label_derivation_is_unchanged():
    for label in re.findall(r'mklabel\s+"(status:[a-z-]+)"',
                            _REGISTER_SCRIPT.read_text()):
        expected = label[len("status:"):].replace("-", " ")
        assert normalize_status_state([label], closed=False) == expected


def test_closed_and_empty_are_unchanged():
    assert normalize_status_state(["status:todo"], closed=True) == "closed"
    assert normalize_status_state([], closed=False) == "none"
    assert normalize_status_state(["bug"], closed=False) == "none"


def test_unranked_status_labels_rank_last_and_tie_alphabetically():
    """An undefined `status:*` label must never out-vote a real state...

    ...and must still derive deterministically, because the board-state check
    (#52) is what reports it and it can only report what derivation produced.
    """
    assert normalize_status_state(
        ["status:todo", "status:aardvark"], closed=False
    ) == "todo"
    assert normalize_status_state(
        ["status:zebra", "status:aardvark"], closed=False
    ) == "aardvark"


# --- loader failure modes -----------------------------------------------------

@pytest.mark.parametrize(
    "table",
    [
        {"edges": []},                    # section absent
        {"precedence": []},               # section empty
        {"precedence": "parked"},         # section not a list
        {"precedence": [1, None]},        # no usable rows
    ],
)
def test_load_precedence_refuses_a_missing_or_malformed_section(tmp_path, table):
    """Refuse rather than fall back to an implicit order.

    A silent fallback to alphabetical would restore the exact bug: a parked
    issue deriving `in progress`. Loud beats plausible.
    """
    path = tmp_path / "transitions.yml"
    path.write_text(yaml.safe_dump(table), encoding="utf-8")
    with pytest.raises(WorkflowError) as exc:
        load_precedence(path)
    assert exc.value.code == "transitions_parse_error"


def test_load_precedence_normalizes_and_dedupes(tmp_path):
    path = tmp_path / "transitions.yml"
    path.write_text(
        yaml.safe_dump({"precedence": ["Parked", " in-progress ", "parked", ""]}),
        encoding="utf-8",
    )
    assert load_precedence(path) == ("parked", "in progress")
