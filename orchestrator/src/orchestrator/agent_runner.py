"""Provider-neutral execution contract consumed by the scheduler."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from .types import EventCallback, TurnResult


class AgentRunner(Protocol):
    """One provider adapter capable of executing a logical agent turn."""

    provider_id: str
    turn_timeout_ms: int
    stall_timeout_ms: int
    max_budget_usd: float | None

    async def run_turn(
        self,
        workspace: Path,
        prompt: str,
        resume_session_id: str | None,
        on_event: EventCallback,
        issue_id: str,
        agent_token: str | None = None,
    ) -> TurnResult: ...


@runtime_checkable
class SummaryCapableRunner(Protocol):
    """A provider that can run the cap-hit summary pass (issue #16).

    A SEPARATE protocol rather than a defaulted method on `AgentRunner`, for one
    reason: the scheduler must be able to *ask* whether a provider can do this,
    and a defaulted method answers "yes" for every adapter that never
    implemented it. Codex has no budget ceiling to report on (`codex_runner.py`
    hard-wires `max_budget_usd = None`; issue #181), so it deliberately does not
    implement this and the scheduler's `isinstance` check skips it.

    `resume_session_id` is REQUIRED here, unlike on `run_turn`: a summary pass
    with nothing to resume has no session to report on and must not be started.
    """

    async def run_summary_turn(
        self,
        workspace: Path,
        prompt: str,
        resume_session_id: str,
        on_event: EventCallback,
        issue_id: str,
        agent_token: str | None = None,
    ) -> TurnResult: ...
