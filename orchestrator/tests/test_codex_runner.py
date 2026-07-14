"""Tests for the standalone Codex CLI AgentRunner adapter."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from orchestrator.codex_runner import CodexRunner
from orchestrator.types import AgentEvent, CodexConfig


FIXTURE = Path(__file__).with_name("fake_codex.py")


def make_cfg(
    *,
    command: str | None = None,
    turn_timeout_ms: int = 5000,
    read_timeout_ms: int = 3000,
    stall_timeout_ms: int = 0,
) -> CodexConfig:
    return CodexConfig(
        command=command or f"{sys.executable} {FIXTURE}",
        turn_timeout_ms=turn_timeout_ms,
        read_timeout_ms=read_timeout_ms,
        stall_timeout_ms=stall_timeout_ms,
    )


class EventRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, AgentEvent]] = []

    def __call__(self, issue_id: str, event: AgentEvent) -> None:
        self.events.append((issue_id, event))

    @property
    def names(self) -> list[str]:
        return [event.event for _, event in self.events]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def test_default_command_is_explicitly_headless_and_workspace_scoped() -> None:
    argv = CodexRunner(CodexConfig())._build_argv(None)

    assert argv == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "workspace-write",
        "--config",
        "sandbox_workspace_write.network_access=true",
        "exec",
        "--ignore-user-config",
        "--color",
        "never",
        "--json",
        "-",
    ]


async def test_success_normalizes_codex_jsonl(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    recorder = EventRecorder()

    result = await CodexRunner(make_cfg()).run_turn(
        workspace, "do the thing", None, recorder, "issue-73"
    )

    assert result.status == "succeeded"
    assert result.session_id == "codex-thread-123"
    assert result.cost_usd == 0.0
    assert result.num_turns == 1
    assert result.usage == {
        "input_tokens": 10,
        "cached_input_tokens": 3,
        "output_tokens": 20,
        "reasoning_output_tokens": 4,
    }
    assert recorder.names == [
        "session_started",
        "notification",
        "notification",
        "turn_completed",
    ]


async def test_fresh_and_resume_argv_and_stdin(
    workspace: Path, monkeypatch, tmp_path: Path
) -> None:
    argv_file = tmp_path / "argv.json"
    stdin_file = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    monkeypatch.setenv("FAKE_CODEX_ARGV_FILE", str(argv_file))
    monkeypatch.setenv("FAKE_CODEX_STDIN_FILE", str(stdin_file))
    runner = CodexRunner(make_cfg())

    await runner.run_turn(workspace, "fresh prompt", None, EventRecorder(), "issue-73")
    assert json.loads(argv_file.read_text()) == [
        "exec", "--ignore-user-config", "--color", "never", "--json", "-"
    ]
    assert stdin_file.read_text() == "fresh prompt"

    await runner.run_turn(
        workspace, "continue", "codex-thread-123", EventRecorder(), "issue-73"
    )
    assert json.loads(argv_file.read_text()) == [
        "exec", "resume", "--ignore-user-config", "--json",
        "codex-thread-123", "-",
    ]
    assert stdin_file.read_text() == "continue"


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("failed", "codex_turn_failed"),
        ("error", "codex_error"),
        ("no_terminal", "port_exit"),
        ("missing_session", "missing_session_id"),
    ],
)
async def test_failure_normalization(
    workspace: Path,
    monkeypatch,
    scenario: str,
    expected_error: str,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", scenario)
    recorder = EventRecorder()

    result = await CodexRunner(make_cfg()).run_turn(
        workspace, "prompt", None, recorder, "issue-73"
    )

    assert result.status == "failed"
    assert result.error == expected_error


async def test_malformed_line_is_reported_and_tolerated(
    workspace: Path, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "malformed")
    recorder = EventRecorder()

    result = await CodexRunner(make_cfg()).run_turn(
        workspace, "prompt", None, recorder, "issue-73"
    )

    assert result.status == "succeeded"
    assert "malformed" in recorder.names


async def test_missing_binary_is_codex_not_found(workspace: Path) -> None:
    result = await CodexRunner(
        make_cfg(command="definitely-not-a-codex-binary")
    ).run_turn(workspace, "prompt", None, EventRecorder(), "issue-73")

    assert result.status == "failed"
    assert result.error == "codex_not_found"


async def test_read_and_turn_timeouts(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "read_timeout")
    read_result = await CodexRunner(
        make_cfg(read_timeout_ms=100, turn_timeout_ms=5000)
    ).run_turn(workspace, "prompt", None, EventRecorder(), "issue-73")
    assert read_result.error == "response_timeout"

    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "turn_timeout")
    turn_result = await CodexRunner(
        make_cfg(read_timeout_ms=1000, turn_timeout_ms=150)
    ).run_turn(workspace, "prompt", None, EventRecorder(), "issue-73")
    assert turn_result.status == "timed_out"
    assert turn_result.error == "turn_timeout"


async def test_cancellation_kills_process_group(
    workspace: Path, monkeypatch, tmp_path: Path
) -> None:
    pid_file = tmp_path / "pid"
    child_file = tmp_path / "child"
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "hang")
    monkeypatch.setenv("FAKE_CODEX_PID_FILE", str(pid_file))
    monkeypatch.setenv("FAKE_CODEX_CHILD_PID_FILE", str(child_file))
    recorder = EventRecorder()
    task = asyncio.create_task(CodexRunner(
        make_cfg(turn_timeout_ms=60000)
    ).run_turn(workspace, "prompt", None, recorder, "issue-73"))

    async def poll(condition, timeout: float = 5.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        while not condition():
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.02)

    await poll(lambda: "session_started" in recorder.names)
    await poll(lambda: pid_file.exists() and child_file.exists())
    wrapper_pid = next(
        event.pid for _, event in recorder.events if event.event == "session_started"
    )
    child_pid = int(child_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    def dead(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    assert wrapper_pid is not None
    await poll(lambda: dead(wrapper_pid))
    await poll(lambda: dead(child_pid))


async def test_agent_token_overlay_preserves_subscription_state(
    workspace: Path, monkeypatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "success")
    monkeypatch.setenv("FAKE_CODEX_ENV_FILE", str(env_file))
    monkeypatch.setenv("CODEX_HOME", "/subscription-state")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")

    result = await CodexRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-73",
        agent_token="ghs-fresh",
    )

    assert result.status == "succeeded"
    assert json.loads(env_file.read_text()) == {
        "GITHUB_TOKEN": "ghs-fresh",
        "GH_TOKEN": "ghs-fresh",
        "NO_COLOR": "1",
        "CODEX_API_KEY": None,
        "OPENAI_API_KEY": None,
        "CODEX_HOME": "/subscription-state",
    }


async def test_stderr_does_not_corrupt_jsonl(workspace: Path, monkeypatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "stderr_noise")
    result = await CodexRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-73"
    )
    assert result.status == "succeeded"


async def test_workspace_must_exist(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await CodexRunner(make_cfg()).run_turn(
            tmp_path / "missing", "prompt", None, EventRecorder(), "issue-73"
        )
