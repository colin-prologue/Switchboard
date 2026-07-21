"""Agent-runner selection boundary consumed by the scheduler."""

from __future__ import annotations

import hashlib
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


class MixedAssignmentRefused(Exception):
    """Raised when a mixed issue has no safe, unambiguous provider assignment."""


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


class MixedRunnerSelector:
    """Stage 6 selector with durable-label precedence and stable hash routing."""

    provider_id = "mixed"

    def select(self, cfg: Config, issue: Issue) -> AgentRunner:
        mixed = cfg.mixed()
        provider_id = self.select_provider(mixed.weights, issue)
        if provider_id == "claude":
            return ClaudeRunner(mixed.claude)
        return CodexRunner(mixed.codex)

    @staticmethod
    def select_provider(weights: dict[str, int], issue: Issue) -> str:
        """Choose one provider without side effects.

        A persisted `provider:*` assignment wins over a later operator label.
        Operator `agent:*` labels are considered only for unassigned issues;
        all remaining issues use a SHA-256 bucket of the immutable node id.
        """
        expected = {"claude", "codex"}
        labels = set(issue.labels)
        assigned = {
            label[len("provider:"):]
            for label in labels
            if label.startswith("provider:")
        }
        if assigned:
            unknown = assigned - expected
            if unknown or len(assigned) != 1:
                raise MixedAssignmentRefused(
                    "conflicting or unsupported durable provider labels: "
                    + ", ".join(sorted(assigned))
                )
            return next(iter(assigned))

        requested = {
            label[len("agent:"):]
            for label in labels
            if label.startswith("agent:")
        }
        if requested:
            unknown = requested - expected
            if unknown or len(requested) != 1:
                raise MixedAssignmentRefused(
                    "conflicting or unsupported operator provider labels: "
                    + ", ".join(sorted(requested))
                )
            return next(iter(requested))

        total = sum(weights[provider_id] for provider_id in ("claude", "codex"))
        bucket = int.from_bytes(
            hashlib.sha256(issue.id.encode("utf-8")).digest(), "big"
        ) % total
        if bucket < weights["claude"]:
            return "claude"
        return "codex"
