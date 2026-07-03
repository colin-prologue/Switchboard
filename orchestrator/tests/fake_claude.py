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


def record_stdin() -> str:
    data = sys.stdin.read()
    stdin_file = os.environ.get("FAKE_STDIN_FILE")
    if stdin_file:
        with open(stdin_file, "w") as f:
            f.write(data)
    return data


def result_line(subtype: str = "success", session_id: str = "sess-123") -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "num_turns": 2,
        "session_id": session_id,
        "permission_denials": [],
    }


def main() -> None:
    scenario = os.environ.get("FAKE_SCENARIO", "success")
    record_argv()

    if scenario == "success":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-123"})
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello there"}]}})
        emit({"type": "user", "message": {"content": [{"type": "text", "text": "ack"}]}})
        emit(result_line("success"))
        return

    if scenario == "resume":
        # argv already recorded above; test inspects FAKE_ARGV_FILE.
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-resumed"})
        emit(result_line("success", session_id="sess-resumed"))
        return

    if scenario == "error_max_turns":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-err"})
        emit(result_line("error_max_turns", session_id="sess-err"))
        sys.exit(1)  # real CLI exits nonzero on error result subtypes

    if scenario == "turn_timeout":
        record_stdin()
        emit({"type": "system", "subtype": "init", "session_id": "sess-slow"})
        # sleep past the tiny turn_timeout_ms configured by the test
        time.sleep(5)
        emit(result_line("success", session_id="sess-slow"))
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
        emit(result_line("success", session_id="sess-mal"))
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
        emit(result_line("success", session_id="sess-stderr"))
        return

    # unknown scenario: fail loudly so tests don't silently pass
    sys.stderr.write(f"unknown FAKE_SCENARIO: {scenario}\n")
    sys.exit(1)


if __name__ == "__main__":
    main()
