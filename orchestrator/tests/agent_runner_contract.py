"""Reusable behavioral assertions for every AgentRunner implementation."""

from __future__ import annotations

import inspect
from pathlib import Path

from orchestrator.agent_runner import AgentRunner
from orchestrator.types import AgentEvent


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, AgentEvent]] = []

    def __call__(self, issue_id: str, event: AgentEvent) -> None:
        self.events.append((issue_id, event))


async def assert_success_contract(
    runner: AgentRunner,
    workspace: Path,
    *,
    expected_provider_id: str,
    expected_session_id: str | None,
) -> None:
    """Exercise the scheduler-facing success contract shared by all adapters."""
    assert isinstance(runner.provider_id, str)
    assert runner.provider_id == expected_provider_id
    assert isinstance(runner.turn_timeout_ms, int)
    assert isinstance(runner.stall_timeout_ms, int)
    assert runner.max_budget_usd is None or isinstance(runner.max_budget_usd, float)
    # Adapter-specific options belong in constructor config; the scheduler call
    # shape stays identical across providers.
    assert list(inspect.signature(runner.run_turn).parameters) == [
        "workspace",
        "prompt",
        "resume_session_id",
        "on_event",
        "issue_id",
        "agent_token",
    ]

    recorder = EventRecorder()
    result = await runner.run_turn(
        workspace,
        "contract prompt",
        resume_session_id=None,
        on_event=recorder,
        issue_id="contract-issue",
        agent_token="contract-token",
    )

    assert result.status == "succeeded"
    assert result.session_id == expected_session_id
    assert recorder.events
    assert all(issue_id == "contract-issue" for issue_id, _ in recorder.events)
    assert recorder.events[-1][1].event == "turn_completed"
