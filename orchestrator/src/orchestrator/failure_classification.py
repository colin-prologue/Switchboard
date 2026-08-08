"""Conservative provider-owned failure classification for Stage 7."""

from __future__ import annotations

import re
from collections.abc import Mapping

from .types import FailureClass


_CLAUDE_CODES: Mapping[str, FailureClass] = {
    "authentication_expired": FailureClass.PROVIDER_AUTHENTICATION,
    "authentication_required": FailureClass.PROVIDER_AUTHENTICATION,
    "plan_limit_reached": FailureClass.PROVIDER_PLAN_LIMIT,
    "usage_limit_reached": FailureClass.PROVIDER_PLAN_LIMIT,
    "credits_exhausted": FailureClass.PROVIDER_CREDITS_EXHAUSTED,
    "rate_limit_exceeded": FailureClass.PROVIDER_RATE_LIMIT,
    "service_unavailable": FailureClass.PROVIDER_UNAVAILABLE,
}

# FORWARD-COMPATIBLE ONLY — unreachable on real output as of codex-cli
# 0.146.0-alpha.3.1: ground truth (issue #109, tests/fixtures/
# codex_cli_auth_401.jsonl) shows real `turn.failed` and `error` events carry
# no `error.code`/`code` field, so `_classify` always falls through to
# `_TEXT_PATTERNS` for Codex. Kept in case a future CLI adds typed codes;
# every entry here must have a text-pattern equivalent derived from captured
# output before it can be considered covered.
_CODEX_CODES: Mapping[str, FailureClass] = {
    "authentication_expired": FailureClass.PROVIDER_AUTHENTICATION,
    "authentication_required": FailureClass.PROVIDER_AUTHENTICATION,
    "usage_limit_reached": FailureClass.PROVIDER_PLAN_LIMIT,
    "credits_exhausted": FailureClass.PROVIDER_CREDITS_EXHAUSTED,
    "insufficient_credits": FailureClass.PROVIDER_CREDITS_EXHAUSTED,
    "rate_limit_exceeded": FailureClass.PROVIDER_RATE_LIMIT,
    "service_unavailable": FailureClass.PROVIDER_UNAVAILABLE,
}

_TEXT_PATTERNS: tuple[tuple[FailureClass, tuple[re.Pattern[str], ...]], ...] = (
    (
        FailureClass.PROVIDER_AUTHENTICATION,
        (
            re.compile(r"\bauthentication (?:has )?(?:expired|invalid)\b"),
            re.compile(r"\blogin (?:has )?expired\b"),
            re.compile(r"\bnot logged in\b"),
            re.compile(r"\bauthentication required\b"),
            # issue #109 ground truth (fixtures/codex_cli_auth_401.jsonl):
            # logged-out codex exec fails with "unexpected status 401
            # Unauthorized: Missing bearer or basic authentication in header".
            re.compile(r"\b401 unauthorized\b"),
            re.compile(r"\bmissing bearer or basic authentication\b"),
        ),
    ),
    (
        FailureClass.PROVIDER_PLAN_LIMIT,
        (
            re.compile(
                r"\b(?:plan|subscription)(?: usage)? limit "
                r"(?:has been )?(?:reached|exceeded)\b"
            ),
            re.compile(r"\busage limit (?:has been )?(?:reached|exceeded)\b"),
        ),
    ),
    (
        FailureClass.PROVIDER_CREDITS_EXHAUSTED,
        (
            re.compile(r"\bcredits? (?:are |have been )?(?:exhausted|depleted)\b"),
            re.compile(r"\bout of credits\b"),
            re.compile(r"\binsufficient credits\b"),
        ),
    ),
    (
        FailureClass.PROVIDER_RATE_LIMIT,
        (
            re.compile(r"\brate limit (?:has been )?(?:reached|exceeded)\b"),
            re.compile(r"\btoo many requests\b"),
        ),
    ),
    (
        FailureClass.PROVIDER_UNAVAILABLE,
        (
            re.compile(r"\b(?:provider|service) (?:is )?(?:temporarily )?unavailable\b"),
        ),
    ),
    # issue #47: Codex `turn.failed` carries only `error.message` (no `code`),
    # so context exhaustion is matched on text here, never via `_CODEX_CODES`.
    # Real message: "Codex ran out of room in the model's context window.
    # Start a new thread or clear earlier history before retrying."
    (
        FailureClass.PROVIDER_CONTEXT_EXHAUSTED,
        (
            re.compile(r"\bran out of room in the model's context window\b"),
        ),
    ),
)


def _classify(
    code: str | None,
    detail: str | None,
    code_map: Mapping[str, FailureClass],
) -> FailureClass:
    normalized_code = code.strip().lower() if isinstance(code, str) else ""
    if normalized_code in code_map:
        return code_map[normalized_code]

    normalized_detail = detail.strip().lower() if isinstance(detail, str) else ""
    for failure_class, patterns in _TEXT_PATTERNS:
        if any(pattern.search(normalized_detail) for pattern in patterns):
            return failure_class
    return FailureClass.WORKER_FAILURE


def classify_claude_failure(
    *, code: str | None = None, detail: str | None = None
) -> FailureClass:
    return _classify(code, detail, _CLAUDE_CODES)


def classify_codex_failure(
    *, code: str | None = None, detail: str | None = None
) -> FailureClass:
    return _classify(code, detail, _CODEX_CODES)
