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
from orchestrator.types import AgentEvent, ClaudeConfig, Continuation, FailureClass

FIXTURE = str(Path(__file__).resolve().parent / "fake_claude.py")
# issue #116 ground truth — real claude-code 2.1.226 output captured logged
# out (see fixtures/README.md). Replayed verbatim; never hand-edited.
AUTH_LOGGED_OUT_CAPTURE = (
    Path(__file__).resolve().parent / "fixtures" / "claude_cli_auth_logged_out.jsonl"
)


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
    assert result.failure_class is None

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


async def test_error_max_turns_is_incomplete_resume(workspace: Path, monkeypatch):
    """issue #47: `error_max_turns` WITH a session id is a benign early stop,
    not a failure — status `incomplete`, resume the same session next turn."""
    monkeypatch.setenv("FAKE_SCENARIO", "error_max_turns")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "incomplete"
    assert result.continuation is Continuation.RESUME_SESSION
    assert result.session_id == "sess-err"
    assert result.failure_class is None
    assert "turn_incomplete" in recorder.names
    assert "turn_failed" not in recorder.names


async def test_error_max_turns_without_session_is_failure(workspace: Path, monkeypatch):
    """issue #47 defensive: `error_max_turns` with NO session id ever learned
    has nothing to resume, so it stays a failure — not `incomplete`."""
    monkeypatch.setenv("FAKE_SCENARIO", "error_max_turns_no_session")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "failed"
    assert result.continuation is None
    assert result.session_id is None
    assert result.error == "error_max_turns"
    assert result.failure_class is FailureClass.WORKER_FAILURE
    assert "turn_failed" in recorder.names


async def test_turn_timeout(workspace: Path, monkeypatch):
    monkeypatch.setenv("FAKE_SCENARIO", "turn_timeout")
    cfg = make_cfg(turn_timeout_ms=200, read_timeout_ms=5000)
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "timed_out"
    assert result.error == "turn_timeout"
    assert result.failure_class is FailureClass.RUNNER_TIMEOUT

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
    assert result.failure_class is FailureClass.RUNNER_TIMEOUT
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
    assert result.failure_class is FailureClass.RUNNER_PROTOCOL


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
    assert result.failure_class is FailureClass.RUNNER_STARTUP


@pytest.mark.parametrize(
    ("code", "detail", "expected"),
    [
        ("authentication_required", "", FailureClass.PROVIDER_AUTHENTICATION),
        ("provider_error", "Usage limit reached", FailureClass.PROVIDER_PLAN_LIMIT),
        ("provider_error", "Credits are exhausted", FailureClass.PROVIDER_CREDITS_EXHAUSTED),
        ("provider_error", "Rate limit exceeded", FailureClass.PROVIDER_RATE_LIMIT),
        ("provider_error", "Service is unavailable", FailureClass.PROVIDER_UNAVAILABLE),
        ("provider_error", "Rate limit policy loaded", FailureClass.WORKER_FAILURE),
    ],
)
async def test_provider_failure_classification(
    workspace: Path,
    monkeypatch,
    code: str,
    detail: str,
    expected: FailureClass,
) -> None:
    monkeypatch.setenv("FAKE_SCENARIO", "provider_error")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_CODE", code)
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_DETAIL", detail)

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-1"
    )

    assert result.status == "failed"
    assert result.error == code
    assert result.failure_class is expected


async def test_provider_diagnostic_does_not_enter_normalized_error(
    workspace: Path, monkeypatch
) -> None:
    secret = "ghs-must-not-enter-turn-result"
    monkeypatch.setenv("FAKE_SCENARIO", "provider_error")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_CODE", "authentication_required")
    monkeypatch.setenv("FAKE_CLAUDE_ERROR_DETAIL", f"Authentication required {secret}")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-1"
    )

    assert result.failure_class is FailureClass.PROVIDER_AUTHENTICATION
    assert secret not in (result.error or "")


async def test_model_result_text_cannot_create_provider_class_on_ungated_result(
    workspace: Path, monkeypatch
) -> None:
    """Narrowed invariant (issue #116): model result text cannot create a
    provider failure class on any result the `is_error` gate does NOT claim.

    Doubles as the branch-ordering regression: the fake's `error_max_turns`
    record carries `is_error: true`, and the #47 branch is ordered ahead of the
    gate, so this stays `incomplete` + RESUME_SESSION and never classifies —
    even though its text would match a provider pattern.
    """
    monkeypatch.setenv("FAKE_SCENARIO", "error_max_turns")
    monkeypatch.setenv(
        "FAKE_CLAUDE_RESULT_TEXT",
        "The application returned rate limit exceeded during its own test.",
    )

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-1"
    )

    assert result.status == "incomplete"
    assert result.continuation is Continuation.RESUME_SESSION
    assert result.session_id == "sess-err"
    assert result.failure_class is None


async def test_logged_out_capture_fails_with_provider_authentication(
    workspace: Path, monkeypatch
) -> None:
    """Issue #116, replaying the committed ground-truth capture verbatim.

    A logged-out claude-code 2.1.226 terminates with `subtype: "success"` and
    `is_error: true`. Pre-#116 the runner branched on subtype alone and
    returned `succeeded`, so classification was never reached: the session was
    burned on a no-op turn and the provider circuit was RESET by the worker's
    clean exit. The outcome now comes from the record's own `is_error`.
    """
    monkeypatch.setenv("FAKE_SCENARIO", "replay_fixture")
    monkeypatch.setenv("FAKE_CLAUDE_FIXTURE", str(AUTH_LOGGED_OUT_CAPTURE))
    recorder = EventRecorder()

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, recorder, "issue-116"
    )

    assert result.status == "failed"
    assert result.error == "api_error"  # terminal_reason, never None
    assert result.failure_class is FailureClass.PROVIDER_AUTHENTICATION
    assert result.session_id == "7c08bd33-23f4-426e-985b-218140f37abc"
    assert "turn_completed" not in recorder.names
    failures = [e for _, e in recorder.events if e.event == "turn_failed"]
    assert len(failures) == 1
    assert failures[0].payload["subtype"] == "success"
    assert failures[0].payload["is_error"] is True


async def test_gated_result_with_unrecognized_text_is_worker_failure(
    workspace: Path, monkeypatch
) -> None:
    """Negative space (issue #116): the gate decides the OUTCOME, never the
    CLASS. An `is_error` result whose text matches no pattern must fail as
    WORKER_FAILURE so it retries with backoff, rather than latching the
    provider circuit the way a blanket is_error => AUTH would on any transient
    API 5xx."""
    monkeypatch.setenv("FAKE_SCENARIO", "gated_success")
    monkeypatch.setenv("FAKE_CLAUDE_TERMINAL_REASON", "api_error")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "API Error: 503 upstream hiccup")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-116"
    )

    assert result.status == "failed"
    assert result.error == "api_error"
    assert result.failure_class is FailureClass.WORKER_FAILURE


async def test_gated_result_text_selects_its_matching_class_by_design(
    workspace: Path, monkeypatch
) -> None:
    """The accepted trust boundary, pinned (issue #116): on a record the gate
    DOES claim, `result` is CLI-authored error text and is classification
    input — so gated text matching a non-auth pattern resolves to that class.
    This is the decision, not an accident: if it is ever reversed, this test is
    the one that must be argued with."""
    monkeypatch.setenv("FAKE_SCENARIO", "gated_success")
    monkeypatch.setenv("FAKE_CLAUDE_TERMINAL_REASON", "api_error")
    monkeypatch.setenv("FAKE_CLAUDE_RESULT_TEXT", "Rate limit exceeded · retry later")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-116"
    )

    assert result.status == "failed"
    assert result.failure_class is FailureClass.PROVIDER_RATE_LIMIT


async def test_gated_result_falls_back_to_terminal_reason_when_result_absent(
    workspace: Path, monkeypatch
) -> None:
    """`detail = result or terminal_reason` — the fallback leg."""
    monkeypatch.setenv("FAKE_SCENARIO", "gated_success")
    monkeypatch.setenv("FAKE_CLAUDE_TERMINAL_REASON", "Authentication required")
    monkeypatch.delenv("FAKE_CLAUDE_RESULT_TEXT", raising=False)

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-116"
    )

    assert result.status == "failed"
    assert result.error == "Authentication required"
    assert result.failure_class is FailureClass.PROVIDER_AUTHENTICATION


async def test_auth_text_in_agent_prose_cannot_fail_a_successful_turn(
    workspace: Path, monkeypatch
) -> None:
    """The decided boundary (issue #116): the auth signal is read from the
    terminal record's OWN fields, never from assistant content. A run whose
    agent merely quotes "Not logged in · Please run /login" has `is_error:
    false` on its result record and is structurally excluded."""
    monkeypatch.setenv("FAKE_SCENARIO", "auth_prose_success")
    recorder = EventRecorder()

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, recorder, "issue-116"
    )

    assert result.status == "succeeded"
    assert result.failure_class is None
    assert "turn_failed" not in recorder.names


def test_fixtures_readme_records_the_is_error_gate() -> None:
    """Standing regression for the README's UNVERIFIED caveat (issue #116):
    the four unverified claude conditions are still caught as failures by the
    outcome gate regardless of their per-condition strings. `grep` is not on
    the worker allowlist, so the check lives here."""
    content = (Path(__file__).resolve().parent / "fixtures" / "README.md").read_text(
        encoding="utf-8"
    )

    assert content.count("is_error") >= 2


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
    line must win over the exit code (no port_exit/claude_not_found remap).
    error_max_turns parses to `incomplete` (issue #47), still not a port_exit."""
    monkeypatch.setenv("FAKE_SCENARIO", "error_max_turns")
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()

    result = await runner.run_turn(workspace, "prompt", None, recorder, "issue-1")

    assert result.status == "incomplete"
    assert result.error == "error_max_turns"


async def test_workspace_must_exist(tmp_path: Path):
    cfg = make_cfg()
    runner = ClaudeRunner(cfg)
    recorder = EventRecorder()
    missing = tmp_path / "does-not-exist"

    with pytest.raises(ValueError):
        await runner.run_turn(missing, "prompt", None, recorder, "issue-1")


# --- agent token injection (issue #10) ----------------------------------------


async def test_agent_token_injected_as_github_and_gh_token(
        workspace: Path, monkeypatch, tmp_path: Path):
    env_file = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENV_FILE", str(env_file))
    monkeypatch.setenv("GITHUB_TOKEN", "operator-token")
    runner = ClaudeRunner(make_cfg())

    result = await runner.run_turn(workspace, "go", None, EventRecorder(),
                                   "issue-1", agent_token="ghs_fresh_mint")

    assert result.status == "succeeded"
    seen = json.loads(env_file.read_text())
    # Both spellings: `gh` reads GH_TOKEN first, git credential helpers and
    # most tooling read GITHUB_TOKEN.
    assert seen == {"GITHUB_TOKEN": "ghs_fresh_mint", "GH_TOKEN": "ghs_fresh_mint"}


async def test_no_agent_token_inherits_orchestrator_env(
        workspace: Path, monkeypatch, tmp_path: Path):
    env_file = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_SCENARIO", "success")
    monkeypatch.setenv("FAKE_ENV_FILE", str(env_file))
    monkeypatch.setenv("GITHUB_TOKEN", "operator-token")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    runner = ClaudeRunner(make_cfg())

    result = await runner.run_turn(workspace, "go", None, EventRecorder(), "issue-1")

    assert result.status == "succeeded"
    seen = json.loads(env_file.read_text())
    assert seen == {"GITHUB_TOKEN": "operator-token", "GH_TOKEN": None}


# --- issue #165: typed provider codes on CLI-authored synthetic records ------


async def test_synthetic_auth_code_latches_even_when_the_gate_claims_it(
    workspace: Path, monkeypatch
) -> None:
    """OAuth expiry: prose no `_TEXT_PATTERNS` entry matches, but the synthetic
    record carries `error: "authentication_failed"`. The typed code decides."""
    monkeypatch.setenv("FAKE_SCENARIO", "synthetic_error")
    monkeypatch.setenv("FAKE_CLAUDE_SYNTHETIC_GATED", "1")
    monkeypatch.setenv("FAKE_CLAUDE_SYNTHETIC_CODE", "authentication_failed")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-165"
    )

    assert result.status == "failed"
    assert result.failure_class is FailureClass.PROVIDER_AUTHENTICATION


async def test_synthetic_auth_code_fails_the_turn_when_the_gate_does_not(
    workspace: Path, monkeypatch
) -> None:
    """The `is_error`-absent shape. AgDR-032's gate never claims this record,
    so before #165 it returned `succeeded` — spending a session and RESETTING
    the circuit on a turn that did no work."""
    monkeypatch.setenv("FAKE_SCENARIO", "synthetic_error")
    monkeypatch.delenv("FAKE_CLAUDE_SYNTHETIC_GATED", raising=False)
    monkeypatch.setenv("FAKE_CLAUDE_SYNTHETIC_CODE", "authentication_failed")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-165"
    )

    assert result.status == "failed"
    assert result.failure_class is FailureClass.PROVIDER_AUTHENTICATION


async def test_synthetic_server_error_is_transient_not_latched(
    workspace: Path, monkeypatch
) -> None:
    """`server_error` must reach a TRANSIENT class: latching a 5xx would take
    the provider down until an operator intervenes (AgDR-032 rejected exactly
    that outcome when it declined a blanket `is_error` rule)."""
    monkeypatch.setenv("FAKE_SCENARIO", "synthetic_error")
    monkeypatch.setenv("FAKE_CLAUDE_SYNTHETIC_CODE", "server_error")
    monkeypatch.setenv(
        "FAKE_CLAUDE_SYNTHETIC_TEXT",
        "API Error: Connection closed mid-response. The response above may be incomplete.",
    )

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-165"
    )

    assert result.status == "failed"
    assert result.failure_class is FailureClass.PROVIDER_UNAVAILABLE


async def test_a_recovered_synthetic_error_does_not_fail_the_turn(
    workspace: Path, monkeypatch
) -> None:
    """The standing code is cleared by any REAL model turn after it. A blip the
    CLI retried through must not fail work that actually landed."""
    monkeypatch.setenv("FAKE_SCENARIO", "synthetic_error")
    monkeypatch.setenv("FAKE_CLAUDE_SYNTHETIC_CODE", "server_error")
    monkeypatch.setenv("FAKE_CLAUDE_SYNTHETIC_RECOVERED", "1")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-165"
    )

    assert result.status == "succeeded"
    assert result.failure_class is None


async def test_a_non_synthetic_record_claiming_a_provider_code_is_ignored(
    workspace: Path, monkeypatch
) -> None:
    """The trust boundary AgDR-032 drew, kept: `model: "<synthetic>"` is what
    makes the code CLI-authored. A real model turn carrying the same field must
    not be able to latch the provider and win itself free retries."""
    monkeypatch.setenv("FAKE_SCENARIO", "spoofed_provider_code")

    result = await ClaudeRunner(make_cfg()).run_turn(
        workspace, "prompt", None, EventRecorder(), "issue-165"
    )

    assert result.status == "succeeded"
    assert result.failure_class is None
