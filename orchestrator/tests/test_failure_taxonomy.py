"""Cap-hit failure taxonomy contract tests (issue #169, split from #16).

These are contract tests, not behaviour tests. The five class strings are a wire
format (#12 parses them out of a rendered report) and the three `Recovery`
tokens are branch keys (#31 routes on them), so every literal below is pinned
deliberately: adding, removing, or renaming one has to red the suite rather than
silently reach a consumer.
"""

from __future__ import annotations

from orchestrator.failure_taxonomy import ApproachTrust, CapFailureClass, Recovery
from orchestrator.types import FailureClass


def test_class_set_is_exactly_addendum_2s_five() -> None:
    assert {m.value for m in CapFailureClass} == frozenset(
        {
            "complexity",
            "iteration",
            "blockage:permission",
            "blockage:dependency",
            "quota",
        }
    )


def test_recovery_and_trust_vocabularies_are_closed() -> None:
    # #31 branches on `recovery`, so these tokens are contract surface, not an
    # internal vocabulary.
    assert {m.value for m in Recovery} == frozenset(
        {"retry_same_approach", "retry_fresh_context", "do_not_retry"}
    )
    assert {m.value for m in ApproachTrust} == frozenset({"yes", "no", "not_applicable"})


def test_table_is_exactly_addendum_2s_recovery_column() -> None:
    # Populated-but-wrong must fail: a uniform (yes, retry_same_approach) across
    # all five members reds here, which a "does every member have attributes?"
    # test would not catch.
    assert {m.value: (m.approach_trusted, m.recovery) for m in CapFailureClass} == {
        "blockage:permission": (ApproachTrust.YES, Recovery.RETRY_SAME_APPROACH),
        "blockage:dependency": (ApproachTrust.YES, Recovery.RETRY_SAME_APPROACH),
        "quota": (ApproachTrust.YES, Recovery.RETRY_SAME_APPROACH),
        "iteration": (ApproachTrust.NO, Recovery.RETRY_FRESH_CONTEXT),
        "complexity": (ApproachTrust.NOT_APPLICABLE, Recovery.DO_NOT_RETRY),
    }


def test_recovery_is_a_discriminated_field_not_prose() -> None:
    # `ApproachTrust`/`Recovery` are StrEnums, so the table assertion above would
    # still pass if the attributes were bare strings. #31 imports these to branch
    # on; pin the types so a future "simplification" to plain strings reds.
    for member in CapFailureClass:
        assert isinstance(member.approach_trusted, ApproachTrust)
        assert isinstance(member.recovery, Recovery)


def test_cap_classes_are_disjoint_from_provider_failure_classes() -> None:
    # Different axes: cap-cause (why the session ran out of road) vs
    # provider-scoped failure (#16 Addendum 3 forbids merging them). This test is
    # what stops a future contributor from "tidying" the two enums together, and
    # what forces a deliberate rename if a new provider code ever collides.
    assert not ({m.value for m in CapFailureClass} & {m.value for m in FailureClass})
