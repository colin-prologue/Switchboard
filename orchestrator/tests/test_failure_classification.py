"""Stage 7 provider failure taxonomy and conservative classifier tests."""

from __future__ import annotations

import pytest

from orchestrator.failure_classification import (
    classify_claude_failure,
    classify_codex_failure,
)
from orchestrator.types import FailureClass


@pytest.mark.parametrize("classifier", [classify_claude_failure, classify_codex_failure])
@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("Authentication has expired; please log in", FailureClass.PROVIDER_AUTHENTICATION),
        ("Your plan usage limit has been reached", FailureClass.PROVIDER_PLAN_LIMIT),
        ("Purchased credits are exhausted", FailureClass.PROVIDER_CREDITS_EXHAUSTED),
        ("Rate limit exceeded", FailureClass.PROVIDER_RATE_LIMIT),
        ("Provider is temporarily unavailable", FailureClass.PROVIDER_UNAVAILABLE),
    ],
)
def test_explicit_provider_messages_are_classified(classifier, detail, expected) -> None:
    assert classifier(detail=detail) is expected


@pytest.mark.parametrize("classifier", [classify_claude_failure, classify_codex_failure])
@pytest.mark.parametrize(
    "detail",
    [
        "Rate limit policy loaded",
        "Credit card updated",
        "Login command is available",
        "Usage metrics unavailable",
        "Implementation failed",
        "",
    ],
)
def test_ambiguous_or_near_match_messages_stay_worker_failure(
    classifier, detail
) -> None:
    assert classifier(detail=detail) is FailureClass.WORKER_FAILURE


@pytest.mark.parametrize(
    ("classifier", "code", "expected"),
    [
        (classify_claude_failure, "authentication_required", FailureClass.PROVIDER_AUTHENTICATION),
        (classify_claude_failure, "plan_limit_reached", FailureClass.PROVIDER_PLAN_LIMIT),
        (classify_codex_failure, "credits_exhausted", FailureClass.PROVIDER_CREDITS_EXHAUSTED),
        (classify_codex_failure, "rate_limit_exceeded", FailureClass.PROVIDER_RATE_LIMIT),
        (classify_codex_failure, "service_unavailable", FailureClass.PROVIDER_UNAVAILABLE),
    ],
)
def test_explicit_provider_codes_are_classified(classifier, code, expected) -> None:
    assert classifier(code=code) is expected


def test_unknown_code_does_not_inherit_a_class_from_its_name() -> None:
    assert (
        classify_codex_failure(code="maybe_rate_limit_related")
        is FailureClass.WORKER_FAILURE
    )


# issue #47: real `codex exec --json` turn.failed carries only error.message
# (no `code`), so context exhaustion is matched on text via _TEXT_PATTERNS.
CODEX_CONTEXT_EXHAUSTED_MESSAGE = (
    "Codex ran out of room in the model's context window. "
    "Start a new thread or clear earlier history before retrying."
)


def test_codex_context_exhaustion_message_classified_via_text() -> None:
    # code=None mirrors real Codex output (no documented `code` field): the
    # match must come from the message text, not _CODEX_CODES.
    assert (
        classify_codex_failure(code=None, detail=CODEX_CONTEXT_EXHAUSTED_MESSAGE)
        is FailureClass.PROVIDER_CONTEXT_EXHAUSTED
    )
    # Claude shares _TEXT_PATTERNS but never emits this Codex-specific string;
    # the classifier is provider-neutral, so it still resolves the same class.
    assert (
        classify_claude_failure(detail=CODEX_CONTEXT_EXHAUSTED_MESSAGE)
        is FailureClass.PROVIDER_CONTEXT_EXHAUSTED
    )
