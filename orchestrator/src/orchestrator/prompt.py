"""Prompt template rendering.

implements: core §12 (Prompt Construction and Context Assembly)
overridden by: spec/SPEC.md §2 (issue fields are GitHub-shaped: identifier is
               the issue number, state is a normalized status:* label or
               "closed")

Renders `workflow.prompt_template` against the normalized `issue` object and
an optional `attempt` counter, using strict Liquid semantics: unknown
variables and unknown filters must fail rendering (core §12.2). We use the
installed `python-liquid` (2.2.2) package: `Environment(undefined=StrictUndefined)`
supplies strict variable checking (the default `Undefined` type silently
renders empty strings instead of raising), and `strict_filters=True` (already
the environment default) makes unknown filters raise `UnknownFilterError`
at parse time.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from liquid import Environment
from liquid.exceptions import LiquidError, UnknownFilterError
from liquid.undefined import StrictUndefined

from .types import Issue, WorkflowError

_DEFAULT_PROMPT = "You are working on an issue from GitHub."

_env = Environment(undefined=StrictUndefined, strict_filters=True)


def render_prompt(template: str, issue: Issue, attempt: int | None) -> str:
    """Render `template` against `issue` and `attempt` (core §12.1-12.4).

    Empty/whitespace-only templates return the minimal default prompt
    (owned adaptation of core §5.4's Linear-worded fallback).

    Note: python-liquid 2.2.2 only raises `UnknownFilterError` eagerly at
    parse time (`from_string`) when the filtered expression is a bare
    identifier; when the left side is a member-access expression (e.g.
    `issue.title | bogus_filter`, the shape our templates actually use),
    the same error is deferred to render time. We catch `UnknownFilterError`
    around both phases and always map it to "template_parse_error" (an
    unknown filter is a template authoring defect, not a data/runtime
    problem) so callers get a stable code regardless of when liquid happens
    to detect it.
    """
    if not template or not template.strip():
        return _DEFAULT_PROMPT

    issue_dict: dict[str, Any] = dataclasses.asdict(issue)

    try:
        parsed = _env.from_string(template)
    except UnknownFilterError as e:
        raise WorkflowError("template_parse_error", str(e)) from e
    except LiquidError as e:
        raise WorkflowError("template_parse_error", str(e)) from e

    try:
        return parsed.render(issue=issue_dict, attempt=attempt)
    except UnknownFilterError as e:
        raise WorkflowError("template_parse_error", str(e)) from e
    except LiquidError as e:
        raise WorkflowError("template_render_error", str(e)) from e
