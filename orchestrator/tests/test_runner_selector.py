"""Stage 3 tests for explicit, injectable agent-runner selection."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.codex_runner import CodexRunner
from orchestrator.runner import ClaudeRunner
from orchestrator.runner_selector import (
    ClaudeOnlyRunnerSelector,
    CodexOnlyRunnerSelector,
    MixedValidationRunnerSelector,
)
from orchestrator.scheduler import Orchestrator
from orchestrator.types import Issue, WorkflowDefinition
from orchestrator.workflow import Config


def _issue() -> Issue:
    return Issue(
        id="node-69",
        identifier="69",
        title="Inject the scheduler runner selector",
        description="body",
        priority=None,
        state="todo",
        branch_name=None,
        url="https://github.com/acme/widgets/issues/69",
        labels=["status:todo", "gate:triage-passed"],
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "execution_config",
    [
        {"claude": {"command": "claude -p --legacy"}},
        {
            "providers": {
                "claude": {
                    "kind": "claude-cli",
                    "command": "claude -p --provider-envelope",
                }
            }
        },
    ],
)
def test_default_selector_constructs_only_claude(
    tmp_path: Path,
    execution_config: dict,
) -> None:
    cfg = Config(
        WorkflowDefinition(config=execution_config, prompt_template="prompt"),
        tmp_path,
    )

    selector = ClaudeOnlyRunnerSelector()
    runner = selector.select(cfg, _issue())

    assert selector.provider_id == "claude"
    assert isinstance(runner, ClaudeRunner)
    assert runner.provider_id == "claude"
    assert runner.cfg == cfg.claude()


def test_codex_only_selector_constructs_only_codex(tmp_path: Path) -> None:
    cfg = Config(
        WorkflowDefinition(
            config={
                "providers": {
                    "codex": {
                        "kind": "codex-cli",
                        "command": "codex --sandbox workspace-write",
                    }
                }
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )

    selector = CodexOnlyRunnerSelector()
    runner = selector.select(cfg, _issue())

    assert selector.provider_id == "codex"
    assert isinstance(runner, CodexRunner)
    assert runner.provider_id == "codex"
    assert runner.cfg == cfg.codex()


class _FakeRunner:
    provider_id = "fake"


class _RecordingSelector:
    provider_id = "claude"

    def __init__(self, runner: _FakeRunner) -> None:
        self.runner = runner
        self.calls: list[tuple[Config, Issue]] = []

    def select(self, cfg: Config, issue: Issue) -> _FakeRunner:
        self.calls.append((cfg, issue))
        return self.runner


class _FailingSelector:
    provider_id = "claude"

    def select(self, cfg: Config, issue: Issue) -> _FakeRunner:
        raise RuntimeError("selector unavailable")


def test_orchestrator_uses_injected_selector(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    runner = _FakeRunner()
    selector = _RecordingSelector(runner)
    orchestrator = Orchestrator(workflow_path, runner_selector=selector)
    cfg = Config(
        WorkflowDefinition(
            config={"claude": {"command": "claude -p"}},
            prompt_template="prompt",
        ),
        tmp_path,
    )
    orchestrator._cfg = cfg
    issue = _issue()

    selected = orchestrator._select_runner(issue)

    assert selected is runner
    assert selector.calls == [(cfg, issue)]


async def test_selector_failure_does_not_claim_or_relabel_issue(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=_FailingSelector(),
    )
    orchestrator._cfg = Config(
        WorkflowDefinition(
            config={
                "agent": {"max_sessions_per_issue": 3},
                "claude": {"command": "claude -p"},
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )
    issue = _issue()

    with pytest.raises(RuntimeError, match="selector unavailable"):
        await orchestrator._dispatch(issue, attempt=None)

    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert issue.labels == ["status:todo", "gate:triage-passed"]
    assert issue.state == "todo"


async def test_mixed_validation_selector_leaves_issue_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedValidationRunnerSelector(),
    )
    orchestrator._cfg = Config(
        WorkflowDefinition(
            config={
                "agent": {"max_sessions_per_issue": 3},
                "providers": {
                    "claude": {"kind": "claude-cli", "command": "claude -p"},
                    "codex": {"kind": "codex-cli", "command": "codex"},
                },
                "routing": {"weights": {"claude": 100, "codex": 0}},
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )
    issue = _issue()
    issue.labels = ["status:todo"]
    orchestrator.sessions_per_issue[issue.id] = 3

    async def _unexpected_tracker_write(*args, **kwargs) -> None:
        pytest.fail("mixed validation mode must not reach a tracker-mutating guard")

    monkeypatch.setattr(orchestrator, "_refuse_missing_marker", _unexpected_tracker_write)
    monkeypatch.setattr(orchestrator, "_park", _unexpected_tracker_write)

    await orchestrator._dispatch(issue, attempt=None)

    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert issue.labels == ["status:todo"]
    assert issue.state == "todo"


def test_selection_after_reload_uses_current_config(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: test-token\n"
        "claude:\n"
        "  command: claude -p --first\n"
        "---\n"
        "prompt\n"
    )
    selector = _RecordingSelector(_FakeRunner())
    orchestrator = Orchestrator(workflow_path, runner_selector=selector)
    orchestrator._load_workflow(initial=True)
    issue = _issue()

    orchestrator._select_runner(issue)
    first_cfg = selector.calls[-1][0]

    workflow_path.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: test-token\n"
        "providers:\n"
        "  claude:\n"
        "    kind: claude-cli\n"
        "    command: claude -p --second\n"
        "---\n"
        "prompt\n"
    )
    stat = workflow_path.stat()
    os.utime(workflow_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    orchestrator._load_workflow(initial=False)

    orchestrator._select_runner(issue)
    second_cfg = selector.calls[-1][0]

    assert first_cfg.claude().command == "claude -p --first"
    assert second_cfg.claude().command == "claude -p --second"
    assert second_cfg is orchestrator._cfg
