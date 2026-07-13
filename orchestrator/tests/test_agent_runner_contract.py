"""Shared AgentRunner contract cases, one per execution adapter."""

from __future__ import annotations

from pathlib import Path

from orchestrator.agent_runner import AgentRunner
from orchestrator.runner import ClaudeRunner
from orchestrator.scheduler import Orchestrator
from orchestrator.types import ClaudeConfig, WorkflowDefinition
from orchestrator.workflow import Config

from agent_runner_contract import assert_success_contract


FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


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

    _, _, runner = orchestrator._components()

    typed_runner: AgentRunner = runner
    assert isinstance(runner, ClaudeRunner)
    assert typed_runner.provider_id == "claude"
