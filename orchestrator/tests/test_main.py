"""CLI provider-mode tests."""

from __future__ import annotations

from pathlib import Path

import orchestrator.main as main_mod
from orchestrator.runner_selector import (
    ClaudeOnlyRunnerSelector,
    CodexOnlyRunnerSelector,
)


class _ImmediateOrchestrator:
    instances: list["_ImmediateOrchestrator"] = []

    def __init__(self, workflow_path: Path, *, runner_selector) -> None:
        self.workflow_path = workflow_path
        self.runner_selector = runner_selector
        self.__class__.instances.append(self)

    async def run(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


def _run_main(
    tmp_path: Path,
    monkeypatch,
    *args: str,
) -> _ImmediateOrchestrator:
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text("prompt")
    _ImmediateOrchestrator.instances.clear()
    monkeypatch.setattr(main_mod, "Orchestrator", _ImmediateOrchestrator)

    assert main_mod.main(["--workflow", str(workflow), *args]) == 0
    return _ImmediateOrchestrator.instances[-1]


def test_cli_defaults_to_claude_only(tmp_path: Path, monkeypatch) -> None:
    orchestrator = _run_main(tmp_path, monkeypatch)

    assert isinstance(orchestrator.runner_selector, ClaudeOnlyRunnerSelector)


def test_cli_codex_mode_is_explicitly_opt_in(tmp_path: Path, monkeypatch) -> None:
    orchestrator = _run_main(tmp_path, monkeypatch, "--provider", "codex")

    assert isinstance(orchestrator.runner_selector, CodexOnlyRunnerSelector)
