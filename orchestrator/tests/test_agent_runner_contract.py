"""Shared AgentRunner contract cases, one per execution adapter."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestrator.agent_runner import AgentRunner
from orchestrator.codex_runner import CodexRunner
from orchestrator.runner import ClaudeRunner
from orchestrator.scheduler import Orchestrator
from orchestrator.types import (
    ClaudeConfig,
    CodexConfig,
    FailureClass,
    Issue,
    WorkflowDefinition,
)
from orchestrator.workflow import Config

from agent_runner_contract import assert_failure_contract, assert_success_contract


FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")
FAKE_CODEX = Path(__file__).with_name("fake_codex.py")


async def test_claude_runner_satisfies_agent_runner_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FAKE_SCENARIO", "success")

    runner = ClaudeRunner(
        ClaudeConfig(
            command=f"python3 {FAKE_CLAUDE}",
            max_turns=5,
            max_budget_usd=None,
            # This is the success contract, not the timeout test. Use the
            # production cold-start bound so loaded CI runners do not turn a
            # subprocess scheduling delay into a protocol failure.
            turn_timeout_ms=30000,
            read_timeout_ms=30000,
            stall_timeout_ms=0,
        )
    )

    await assert_success_contract(
        runner,
        workspace,
        expected_provider_id="claude",
        expected_session_id="sess-123",
    )


async def test_codex_runner_satisfies_agent_runner_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    runner = CodexRunner(
        CodexConfig(
            command=f"{sys.executable} {FAKE_CODEX}",
            turn_timeout_ms=30000,
            read_timeout_ms=30000,
            stall_timeout_ms=0,
        )
    )

    await assert_success_contract(
        runner,
        workspace,
        expected_provider_id="codex",
        expected_session_id="codex-thread-123",
    )


async def test_claude_runner_satisfies_typed_failure_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FAKE_SCENARIO", "provider_error")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_CODE", "rate_limit_exceeded")
    runner = ClaudeRunner(
        ClaudeConfig(
            command=f"python3 {FAKE_CLAUDE}",
            max_turns=5,
            max_budget_usd=None,
            turn_timeout_ms=30000,
            read_timeout_ms=30000,
            stall_timeout_ms=0,
        )
    )

    await assert_failure_contract(
        runner,
        workspace,
        expected_provider_id="claude",
        expected_failure_class=FailureClass.PROVIDER_RATE_LIMIT,
    )


async def test_codex_runner_satisfies_typed_failure_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "provider_error")
    monkeypatch.setenv("FAKE_CODEX_ERROR_CODE", "usage_limit_reached")
    runner = CodexRunner(
        CodexConfig(
            command=f"{sys.executable} {FAKE_CODEX}",
            turn_timeout_ms=30000,
            read_timeout_ms=30000,
            stall_timeout_ms=0,
        )
    )

    await assert_failure_contract(
        runner,
        workspace,
        expected_provider_id="codex",
        expected_failure_class=FailureClass.PROVIDER_PLAN_LIMIT,
    )


def test_scheduler_components_still_construct_claude_only(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(workflow_path)
    orchestrator._cfg = Config(
        WorkflowDefinition(
            config={
                "tracker": {"kind": "github", "repo": "acme/widgets"},
                "claude": {"command": "claude -p"},
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )

    runner = orchestrator._select_runner(
        Issue(
            id="node-1",
            identifier="1",
            title="Contract test",
            description=None,
            priority=None,
            state="todo",
            branch_name=None,
            url=None,
        )
    )

    typed_runner: AgentRunner = runner
    assert isinstance(runner, ClaudeRunner)
    assert typed_runner.provider_id == "claude"
