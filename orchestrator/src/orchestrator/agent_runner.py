"""Provider-neutral execution contract consumed by the scheduler."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .types import EventCallback, TurnResult


class AgentRunner(Protocol):
    """One provider adapter capable of executing a logical agent turn."""

    provider_id: str

    async def run_turn(
        self,
        workspace: Path,
        prompt: str,
        resume_session_id: str | None,
        on_event: EventCallback,
        issue_id: str,
        agent_token: str | None = None,
    ) -> TurnResult: ...
