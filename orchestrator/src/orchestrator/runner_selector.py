"""Agent-runner selection boundary consumed by the scheduler."""

from __future__ import annotations

import hashlib
from typing import Protocol

from .agent_runner import AgentRunner
from .codex_runner import CodexRunner
from .runner import ClaudeRunner
from .types import CodexConfig, Issue
from .workflow import Config


def _gate_c_repo(cfg: Config) -> str:
    """The repo whose merge gate this project's stance hands to an agent, or ""
    when a human owns it. Both values come from the same tracker block, so the
    guard can never be told a repo the scheduler is not working."""
    tracker = cfg.tracker()
    return tracker.repo if tracker.agent_owns_gate_c() else ""


class AgentRunnerSelector(Protocol):
    """Select one execution adapter for a dispatchable issue."""

    provider_id: str

    def select(self, cfg: Config, issue: Issue) -> AgentRunner: ...


class AssignmentRefused(Exception):
    """Raised when an issue has no safe provider assignment.

    The scheduler catches this base, not its subclasses: every refusal here has
    the same handling — leave the issue untouched, log `assignment_refused`,
    dispatch nothing.
    """


class MixedAssignmentRefused(AssignmentRefused):
    """Raised when a mixed issue has no safe, unambiguous provider assignment."""


class CodexGuardUnavailable(AssignmentRefused):
    """Raised when Codex would be routed to a project whose stance hands Gate C
    to an agent (issue #135, the named Codex residual of #133 / AgDR-036).

    A Codex session has NO PreToolUse surface: `CodexRunner._build_argv` emits
    no settings/hook/guard flag of any kind, so `guard.py` never runs and none
    of the enumerated Gate-C shapes are denied. The Claude guard's job on an
    agent-owned gate is not "permit merging" — the stance already permits that.
    It is to BOUND the merge to the granting project's own repository
    (`guard._merge_stays_inside_this_project`), because one App installation
    token also reaches the human-gated repos in the same installation. Codex
    carries no such bound, so this combination would hand an unbounded merge
    right across project boundaries.

    Refused at dispatch rather than left as a residual named in a record nobody
    re-reads (AgDR-048): a mechanism is the floor, not a paragraph.
    """


def _codex_runner(cfg: Config, codex_cfg: CodexConfig) -> CodexRunner:
    """The single construction point for `CodexRunner`, so the guard-surface
    refusal cannot be reached by adding a second selector that forgets it."""
    gate_c_repo = _gate_c_repo(cfg)
    if gate_c_repo:
        raise CodexGuardUnavailable(
            "codex has no PreToolUse guard surface, so it cannot be routed to "
            f"{gate_c_repo}, whose stance hands Gate C to an agent "
            "(issue #135; route this issue to claude or move the project's "
            "handoff back to the human gate)"
        )
    return CodexRunner(codex_cfg)


class ClaudeOnlyRunnerSelector:
    """Stage 3 production selector: every issue still runs with Claude."""

    provider_id = "claude"

    def select(self, cfg: Config, issue: Issue) -> AgentRunner:
        del issue
        return ClaudeRunner(cfg.claude(), _gate_c_repo(cfg))


class CodexOnlyRunnerSelector:
    """Stage 5 canary selector: every issue runs with the Codex CLI."""

    provider_id = "codex"

    def select(self, cfg: Config, issue: Issue) -> AgentRunner:
        del issue
        return _codex_runner(cfg, cfg.codex())


class MixedRunnerSelector:
    """Stage 6 selector with durable-label precedence and stable hash routing."""

    provider_id = "mixed"

    def select(self, cfg: Config, issue: Issue) -> AgentRunner:
        mixed = cfg.mixed()
        provider_id = self.select_provider(mixed.weights, issue)
        if provider_id == "claude":
            return ClaudeRunner(mixed.claude, _gate_c_repo(cfg))
        return _codex_runner(cfg, mixed.codex)

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
