"""Agent-runner selection boundary consumed by the scheduler."""

from __future__ import annotations

from typing import Protocol

from .agent_runner import AgentRunner
from .codex_runner import CodexRunner
from .runner import ClaudeRunner
from .types import Issue
from .workflow import Config


class AgentRunnerSelector(Protocol):
    """Select one execution adapter for a dispatchable issue."""

    provider_id: str

    def select(self, cfg: Config, issue: Issue) -> AgentRunner: ...


class ClaudeOnlyRunnerSelector:
    """Stage 3 production selector: every issue still runs with Claude."""

    provider_id = "claude"

    def select(self, cfg: Config, issue: Issue) -> AgentRunner:
        del issue
        return ClaudeRunner(cfg.claude())


class CodexOnlyRunnerSelector:
    """Stage 5 canary selector: every issue runs with the Codex CLI."""

    provider_id = "codex"

    def select(self, cfg: Config, issue: Issue) -> AgentRunner:
        del issue
        return CodexRunner(cfg.codex())
