#!/usr/bin/env python3
"""Fixture fake `claude -p --output-format stream-json` agent for runner tests.

Not part of the shipped orchestrator; test-only. Scenario is selected via the
FAKE_SCENARIO env var. Some scenarios also read FAKE_ARGV_FILE / FAKE_STDIN_FILE
env vars to record what the runner passed through (argv, resume flags, prompt
delivered on stdin) so tests can assert on them.

Usage (invoked by ClaudeRunner as `bash -lc "python3 fake_claude.py [...args set via cfg.command]"`):
    python3 fake_claude.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def record_argv() -> None:
    argv_file = os.environ.get("FAKE_ARGV_FILE")
    if argv_file:
        with open(argv_file, "w") as f:
            json.dump(sys.argv[1:], f)


def record_env() -> None:
    env_file = os.environ.get("FAKE_ENV_FILE")
    if env_file:
        with open(env_file, "w") as f:
            json.dump({k: os.environ.get(k) for k in ("GITHUB_TOKEN", "GH_TOKEN")}, f)


def record_stdin() -> str:
    data = sys.stdin.read()
    stdin_file = os.environ.get("FAKE_STDIN_FILE")
    if stdin_file:
        with open(stdin_file, "w") as f:
            f.write(data)
    return data


def result_line(
    subtype: str = "success",
    session_id: str = "sess-123",
    *,
    is_error: bool,
) -> dict:
    """One terminal `result` record.

    `is_error` is EXPLICIT and never derived (issue #116): ground truth
    (fixtures/claude_cli_auth_logged_out.jsonl) is a record with
    subtype "success" AND is_error true, so `is_error = subtype != "success"`
    is a relationship the real CLI disproves. Each caller states its own value.
    """
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "num_turns": 2,
        "session_id": session_id,
        "permission_denials": [],
    }


def main() -> None:
    scenario = os.environ.get("FAKE_SCENARIO", "success")
    record_argv()
    record_env()

    if scenario == "success":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-123"})
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello there"}]}})
        emit({"type": "user", "message": {"content": [{"type": "text", "text": "ack"}]}})
        emit(result_line("success", is_error=False))
        return

    if scenario == "resume":
        # argv already recorded above; test inspects FAKE_ARGV_FILE.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-resumed"})
        emit(result_line("success", session_id="sess-resumed", is_error=False))
        return

    if scenario == "error_max_turns":
        # OBS-023 conformance (issue #47): the real CLI emits its session id on
        # init and preserves it on the `error_max_turns` result, so the runner
        # can resume. The fake models the same — a valid session id rides the
        # early-stop result — which is what makes this the `incomplete` case.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-err"})
        # UNVERIFIED: no capture of a real `error_max_turns` result exists, so
        # whether the real CLI sets is_error there is unknown. True is the
        # WORST CASE — and it is what makes the issue #116 branch ordering
        # bite: this record must still be `incomplete` + RESUME_SESSION.
        payload = result_line("error_max_turns", session_id="sess-err", is_error=True)
        if result_text := os.environ.get("FAKE_CLAUDE_RESULT_TEXT"):
            payload["result"] = result_text
        emit(payload)
        sys.exit(1)  # real CLI exits nonzero on error result subtypes

    if scenario == "error_max_turns_no_session":
        # Defensive path (issue #47): `error_max_turns` with NO session id ever
        # learned (no init line, no session_id on the result). Nothing to
        # resume, so this stays a failure, not `incomplete`.
        record_stdin()
        payload = result_line("error_max_turns", is_error=True)  # UNVERIFIED, as above
        payload.pop("session_id")
        emit(payload)
        sys.exit(1)

    if scenario == "provider_error":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-provider"})
        payload = result_line(
            os.environ.get("FAKE_CLAUDE_ERROR_CODE", "provider_error"),
            session_id="sess-provider",
            is_error=True,  # UNVERIFIED — no capture of a real error subtype
        )
        payload["error"] = {
            "message": os.environ.get("FAKE_CLAUDE_ERROR_DETAIL", "provider failed")
        }
        emit(payload)
        sys.exit(1)

    if scenario == "gated_success":
        # issue #116: a result the `is_error` gate claims — subtype "success"
        # with is_error true, the shape ground truth shows. SYNTHETIC on
        # purpose: the text is supplied per-test so the class-derivation step
        # (including the no-pattern negative space) can be exercised
        # independently of the one real capture, which is replayed verbatim by
        # `replay_fixture` below.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-gated"})
        payload = result_line("success", session_id="sess-gated", is_error=True)
        payload["terminal_reason"] = os.environ.get(
            "FAKE_CLAUDE_TERMINAL_REASON", "api_error"
        )
        if result_text := os.environ.get("FAKE_CLAUDE_RESULT_TEXT"):
            payload["result"] = result_text
        emit(payload)
        sys.exit(1)

    if scenario == "auth_prose_success":
        # issue #116 boundary: the agent merely QUOTES auth-failure text in its
        # own prose while the run genuinely succeeds. The terminal record says
        # is_error false, so the gate never claims it.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-prose"})
        emit({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "The ticket says: Not logged in · Please run /login"}
        ]}})
        emit(result_line("success", session_id="sess-prose", is_error=False))
        return

    if scenario == "synthetic_error":
        # issue #165: a CLI-AUTHORED synthetic error record. Ground truth for
        # the `model: "<synthetic>"` + top-level typed `error` shape is
        # fixtures/claude_cli_auth_logged_out.jsonl; the CODE VALUES
        # ("authentication_failed", "server_error") are transcript-derived, not
        # stream-json captures (fixtures/README.md). `is_error` on the terminal
        # record is supplied per-test because which of the two shapes an
        # OAuth-expiry run produces is UNVERIFIED.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-synth"})
        emit({
            "type": "assistant",
            "error": os.environ.get("FAKE_CLAUDE_SYNTHETIC_CODE", "authentication_failed"),
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": os.environ.get(
                    "FAKE_CLAUDE_SYNTHETIC_TEXT",
                    "Failed to authenticate: OAuth session expired and could not be refreshed",
                )}],
            },
        })
        if os.environ.get("FAKE_CLAUDE_SYNTHETIC_RECOVERED"):
            # A REAL model turn after the synthetic error: the CLI retried and
            # got through, so the standing error must not fail the turn.
            emit({"type": "assistant", "message": {
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "Recovered; work continued."}],
            }})
        emit(result_line(
            os.environ.get("FAKE_CLAUDE_SYNTHETIC_SUBTYPE", "success"),
            session_id="sess-synth",
            is_error=bool(os.environ.get("FAKE_CLAUDE_SYNTHETIC_GATED")),
        ))
        sys.exit(0)

    if scenario == "spoofed_provider_code":
        # issue #165 trust boundary: a REAL model turn carrying the same
        # top-level `error` field a synthetic record would. `model` is
        # CLI-assigned, so this is the shape a model cannot forge — and the
        # harvest must ignore it.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-spoof"})
        emit({
            "type": "assistant",
            "error": "authentication_failed",
            "message": {
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "Pretending to be logged out."}],
            },
        })
        emit(result_line("success", session_id="sess-spoof", is_error=False))
        sys.exit(0)

    if scenario == "replay_fixture":
        # Stream a committed ground-truth capture byte-for-byte (mirrors
        # fake_codex.py). Never re-encode the lines as dicts — the point is that
        # the runner sees exactly what the real CLI emitted
        # (tests/fixtures/README.md).
        record_stdin()
        sys.stdout.buffer.write(open(os.environ["FAKE_CLAUDE_FIXTURE"], "rb").read())
        sys.stdout.buffer.flush()
        sys.exit(1)  # real logged-out run exits 1 (fixtures/README.md)

    if scenario == "turn_timeout":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-slow"})
        # sleep past the tiny turn_timeout_ms configured by the test
        time.sleep(5)
        emit(result_line("success", session_id="sess-slow", is_error=False))
        return

    if scenario == "hang":
        # Emit init (so the runner learns the pid/session), then hang far past
        # any test timeout. Used to prove cancellation kills the whole PROCESS
        # GROUP, not just the leader. bash execs this single command, so our
        # own pid == the runner's proc.pid; a proc.kill() regression would kill
        # that shared process and look correct. To distinguish killpg from a
        # bare proc.kill(), spawn a DISTINCT child in the same process group:
        # only os.killpg reaps it. The test asserts this grandchild dies.
        record_stdin()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        child_pid_file = os.environ.get("FAKE_CHILD_PID_FILE")
        if child_pid_file:
            with open(child_pid_file, "w") as f:
                f.write(str(child.pid))
        pid_file = os.environ.get("FAKE_PID_FILE")
        if pid_file:
            with open(pid_file, "w") as f:
                f.write(str(os.getpid()))
        emit({"type": "system", "subtype": "init", "session_id": "sess-hang"})
        time.sleep(300)
        return

    if scenario == "read_timeout":
        # sleep before ANY output, past the tiny read_timeout_ms configured by the test
        time.sleep(5)
        emit({"type": "system", "subtype": "init", "session_id": "sess-never"})
        return

    if scenario == "malformed":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-mal"})
        sys.stdout.write("not json at all {{{\n")
        sys.stdout.flush()
        emit(result_line("success", session_id="sess-mal", is_error=False))
        return

    if scenario == "no_result":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-noresult"})
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "just chatting"}]}})
        # exits 0 without ever emitting a result line
        return

    if scenario == "stderr_noise":
        record_stdin()
        sys.stderr.write("some diagnostic noise\nmore noise\n")
        sys.stderr.flush()
        emit({"type": "system", "subtype": "init", "session_id": "sess-stderr"})
        emit(result_line("success", session_id="sess-stderr", is_error=False))
        return

    # unknown scenario: fail loudly so tests don't silently pass
    sys.stderr.write(f"unknown FAKE_SCENARIO: {scenario}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
