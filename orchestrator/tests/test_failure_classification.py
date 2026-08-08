"""Stage 7 provider failure taxonomy and conservative classifier tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.codex_runner import _error_code, _error_text
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


# issue #109: fixture-driven ground truth — real codex-cli 0.146.0-alpha.3.1
# output captured logged-out (see fixtures/README.md). Never hand-edit the
# fixture; these tests assert against the REAL event shapes and strings.
_FIXTURE = Path(__file__).parent / "fixtures" / "codex_cli_auth_401.jsonl"


def _fixture_events() -> list[dict]:
    return [
        json.loads(line)
        for line in _FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_no_real_codex_event_carries_an_error_code() -> None:
    # Finding 1 of #109: _CODEX_CODES is unreachable — every real event lacks
    # both `error.code` and top-level `code`. If a future CLI version starts
    # emitting codes, this test fails on the refreshed fixture and _CODEX_CODES
    # coverage must be re-evaluated (see comment on _CODEX_CODES).
    events = _fixture_events()
    assert events, "fixture is empty"
    for event in events:
        assert _error_code(event) is None, event


def test_real_codex_turn_failed_auth_message_opens_auth_class() -> None:
    # The terminal turn.failed from a real logged-out run must classify as
    # PROVIDER_AUTHENTICATION via text (pre-#109 it fell to WORKER_FAILURE and
    # the provider circuit never opened on a real auth outage).
    events = _fixture_events()
    turn_failed = [e for e in events if e.get("type") == "turn.failed"]
    assert len(turn_failed) == 1
    detail = _error_text(turn_failed[0])
    assert "401 unauthorized" in detail.lower()
    assert (
        classify_codex_failure(code=_error_code(turn_failed[0]), detail=detail)
        is FailureClass.PROVIDER_AUTHENTICATION
    )


def test_real_codex_intermediate_error_events_use_top_level_message() -> None:
    # Finding: real intermediate `error` events carry a TOP-LEVEL `message`
    # (not a nested error object). _error_text must extract it, and the
    # extracted text must classify to the auth class, since the runner breaks
    # and classifies on the first `error` event it sees.
    events = _fixture_events()
    intermediate = [e for e in events if e.get("type") == "error"]
    assert intermediate, "fixture has no intermediate error events"
    for event in intermediate:
        assert "error" not in event, "real error events are not nested"
        detail = _error_text(event)
        assert detail, event
        assert (
            classify_codex_failure(code=_error_code(event), detail=detail)
            is FailureClass.PROVIDER_AUTHENTICATION
        )
