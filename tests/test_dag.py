import pytest

from sb import dag, store
from tests.helpers import make_task


def test_acyclic_passes():
    dag.assert_acyclic({"A": ["B"], "B": ["C"], "C": []})


def test_cycle_raises():
    with pytest.raises(dag.CycleError):
        dag.assert_acyclic({"A": ["B"], "B": ["A"]})


def test_self_edge_raises():
    with pytest.raises(dag.CycleError):
        dag.assert_acyclic({"A": ["A"]})


def test_unknown_deps_are_leaves():
    dag.assert_acyclic({"A": ["DONE-ELSEWHERE"]})


def test_assert_addition_ok_catches_ancestor_cycle(lay):
    parent = make_task("PLAN-001/PH-1/T-1")
    store.write_task(lay, "active", parent)
    # a research task that (illegally) depends on its own parent, while the
    # parent will gain a dependency on it: A -> R -> A
    research = make_task("PLAN-001/PH-1/T-1.R1",
                         context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    with pytest.raises(dag.CycleError):
        dag.assert_addition_ok(lay, research,
                               extra_parent_deps=("PLAN-001/PH-1/T-1",
                                                  ["PLAN-001/PH-1/T-1.R1"]))


def test_assert_addition_ok_passes_clean_spawn(lay):
    parent = make_task("PLAN-001/PH-1/T-1")
    store.write_task(lay, "active", parent)
    research = make_task("PLAN-001/PH-1/T-1.R1")
    dag.assert_addition_ok(lay, research,
                           extra_parent_deps=("PLAN-001/PH-1/T-1",
                                              ["PLAN-001/PH-1/T-1.R1"]))
