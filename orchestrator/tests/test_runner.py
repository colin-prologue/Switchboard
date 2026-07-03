"""Tests for the Claude CLI execution adapter.

implements: core §17.5 (Coding-Agent App-Server Client test matrix) / overridden
by: SPEC.md §1 (adapted to the Claude CLI stream-json binding, exercised
against tests/fake_claude.py instead of a real `claude` binary or Codex
app-server).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from orchestrator.runner import ClaudeRunner
from orchestrator.types import AgentEvent, ClaudeConfig

FIXTURE = str(Path(__file__).resolve().parent / "fake_claude.py")


def make_cfg(
    *,
    max_turns: int = 5,
    max_budget_usd: float | None = None,
    turn_timeout_ms: int = 3600000,
    read_timeout_ms: int = 5000,
    stall_timeout_ms: int = 300000,
) -> ClaudeConfig:
    return ClaudeConfig(
        command=f"python3 {FIXTURE}",
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
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
        return [e.event for _, e in self.events]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


async def test_success_path(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "do the thing", None, recorder, "issue-1")

    assert result.status == "succeeded"
    assert result.session_id == "sess-123"
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.usage == {"input_tokens": 10, "output_tokens": 20}
    assert result.num_turns == 2

    assert recorder.names == [
        "session_started",
        "notification",
        "notification",
        "turn_completed",
    ]
    assert all(e.timestamp is not None for _, e in recorder.events)
    assert all(e.pid for _, e in recorder.events)
    assert recorder.events[0][1].payload["session_id"] == "sess-123"


async def test_resume_passes_flag(workspace: Path, monkeypatch, tmp_path: Path):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_SCENARIO", "resume")
    monkeypatch.setenv("FAKE_ARGV_FILE", str(argv_file))
    cfg = make_cfg(max_turns=7, max_budget_usd=1.5)
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "continue", "sess-abc", recorder, "issue-1")

    assert result.status == "succeeded"
    assert result.session_id == "sess-resumed"

    argv = json.loads(argv_file.read_text())
    joined = " ".join(argv)
    assert "--max-turns 7" in joined
    assert "--max-budget-usd 1.5" in joined
    assert "--resume sess-abc" in joined


async def test_max_turns_and_budget_without_resume(workspace: Path, monkeypatch, tmp_path: Path):
    argv_file = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ARGV_FILE", str(argv_file))
    cfg = make_cfg(max_turns=3, max_budget_usd=2.0)
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    argv = json.loads(argv_file.read_text())
    joined = " ".join(argv)
    assert "--max-turns 3" in joined
    assert "--max-budget-usd 2.0" in joined
    assert "--resume" not in joined


async def test_error_result_subtype(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "error_max_turns")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "failed"
    assert result.error == "error_max_turns"
    assert result.session_id == "sess-err"
    assert "turn_failed" in recorder.names


async def test_turn_timeout(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "turn_timeout")
    cfg = make_cfg(turn_timeout_ms=200, read_timeout_ms=5000)
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "timed_out"
    assert result.error == "turn_timeout"

    # process must actually be dead afterwards
    pid = recorder.events[-1][1].pid
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_read_timeout(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "read_timeout")
    cfg = make_cfg(read_timeout_ms=200, turn_timeout_ms=5000)
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "failed"
    assert result.error == "response_timeout"
    assert "startup_failed" in recorder.names


async def test_malformed_line_tolerated(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "malformed")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "succeeded"
    assert "malformed" in recorder.names
    assert recorder.names[-1] == "turn_completed"


async def test_no_result_line_is_port_exit(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "no_result")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "failed"
    assert result.error == "port_exit"


async def test_nonexistent_command_is_claude_not_found(workspace: Path):
    cfg = ClaudeConfig(
        command="this-binary-does-not-exist-xyz --flag",
        max_turns=5,
        max_budget_usd=None,
        turn_timeout_ms=3600000,
        read_timeout_ms=2000,
        stall_timeout_ms=300000,
    )
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "failed"
    assert result.error == "claude_not_found"


async def test_prompt_delivered_via_stdin(workspace: Path, monkeypatch, tmp_path: Path):
    stdin_file = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_STDIN_FILE", str(stdin_file))
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    await runner.run_turn(workspace, "the exact prompt text", None, recorder, "issue-1")

    assert stdin_file.read_text() == "the exact prompt text"


async def test_stderr_noise_does_not_corrupt_parsing(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "stderr_noise")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "succeeded"
    assert result.session_id == "sess-stderr"


async def test_cancellation_kills_process_group(workspace: Path, monkeypatch, tmp_path: Path):
    """Cancelling a worker mid-turn (stall/reconciliation/shutdown, core §8.5)
    must SIGKILL the agent's whole PROCESS GROUP, not just the leader. The
    'hang' scenario sleeps 300s AND spawns a distinct child in the same group;
    only os.killpg reaps that child, so a proc.kill()-only regression leaves it
    alive and fails the pid-dead poll — distinguishing group-kill from a bare
    leader kill (which bash's exec makes indistinguishable on the leader pid)."""
    pid_file = tmp_path / "agent.pid"
    child_pid_file = tmp_path / "agent-child.pid"
    monkeypatch.setenv("FAKE_SCENARIO", "hang")
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    monkeypatch.setenv("FAKE_CHILD_PID_FILE", str(child_pid_file))
    cfg = make_cfg(turn_timeout_ms=60000, read_timeout_ms=10000)
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    task = asyncio.create_task(
        runner.run_turn(workspace, "prompt", None, recorder, "issue-1"))

    async def poll(cond, timeout=5.0):
        deadline = asyncio.get_event_loop().time() + timeout
        while not cond():
            assert asyncio.get_event_loop().time() < deadline, "condition not met"
            await asyncio.sleep(0.02)

    # wait for init so the subprocess is definitely up and its pid is known
    await poll(lambda: "session_started" in recorder.names)
    wrapper_pid = next(e.pid for _, e in recorder.events
                       if e.event == "session_started")
    assert wrapper_pid is not None
    await poll(lambda: pid_file.exists() and child_pid_file.exists())
    agent_pid = int(pid_file.read_text())
    agent_child_pid = int(child_pid_file.read_text())
    # The descendant must be a DISTINCT process — otherwise killpg and a bare
    # proc.kill() are indistinguishable and the group-kill claim is untested.
    assert agent_child_pid != wrapper_pid

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    def dead(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True

    # leader and its distinct child are both reaped only if the GROUP was
    # killed; each is reparented to init after SIGKILL — poll briefly for both.
    await poll(lambda: dead(wrapper_pid))
    await poll(lambda: dead(agent_child_pid))


async def test_error_scenario_exits_nonzero_result_still_parsed(workspace: Path, monkeypatch):
    """The real CLI exits nonzero on error result subtypes; the parsed result
    line must win over the exit code (no port_exit/claude_not_found remap)."""
    monkeypatch.setenv("FAKE_SCENARIO", "error_max_turns")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "failed"
    assert result.error == "error_max_turns"


async def test_workspace_must_exist(tmp_path: Path):
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ValueError):
        await runner.run_turn(missing, "prompt", None, recorder, "issue-1")
