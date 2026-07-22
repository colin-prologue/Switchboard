#!/usr/bin/env python3
"""Deterministic JSONL fixture for CodexRunner subprocess tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


scenario = os.environ.get("FAKE_CODEX_SCENARIO", "success")

if path := os.environ.get("FAKE_CODEX_ARGV_FILE"):
    Path(path).write_text(json.dumps(sys.argv[1:]))
if path := os.environ.get("FAKE_CODEX_STDIN_FILE"):
    Path(path).write_text(sys.stdin.read())
else:
    sys.stdin.read()
if path := os.environ.get("FAKE_CODEX_ENV_FILE"):
    Path(path).write_text(json.dumps({
        "GITHUB_TOKEN": os.environ.get("GITHUB_TOKEN"),
        "GH_TOKEN": os.environ.get("GH_TOKEN"),
        "NO_COLOR": os.environ.get("NO_COLOR"),
        "CODEX_API_KEY": os.environ.get("CODEX_API_KEY"),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY"),
        "CODEX_HOME": os.environ.get("CODEX_HOME"),
    }))

if scenario == "read_timeout":
    time.sleep(300)
elif scenario == "missing_session":
    emit({"type": "turn.completed", "usage": {"input_tokens": 1}})
elif scenario == "no_terminal":
    emit({"type": "thread.started", "thread_id": "codex-no-terminal"})
elif scenario == "failed":
    emit({"type": "thread.started", "thread_id": "codex-failed"})
    emit({"type": "turn.failed", "error": {"message": "model failed"}})
elif scenario == "error":
    emit({"type": "thread.started", "thread_id": "codex-error"})
    emit({"type": "error", "message": "transport failed"})
elif scenario == "provider_error":
    emit({"type": "thread.started", "thread_id": "codex-provider-error"})
    emit({
        "type": "error",
        "error": {
            "code": os.environ.get("FAKE_CODEX_ERROR_CODE", "provider_error"),
            "message": os.environ.get("FAKE_CODEX_ERROR_DETAIL", "provider failed"),
        },
    })
elif scenario in {"turn_timeout", "hang"}:
    emit({"type": "thread.started", "thread_id": "codex-hang"})
    if path := os.environ.get("FAKE_CODEX_PID_FILE"):
        Path(path).write_text(str(os.getpid()))
    if path := os.environ.get("FAKE_CODEX_CHILD_PID_FILE"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        Path(path).write_text(str(child.pid))
    time.sleep(300)
else:
    if scenario == "malformed":
        print("{not-json", flush=True)
        print("[]", flush=True)
    if scenario == "stderr_noise":
        print("diagnostic noise", file=sys.stderr, flush=True)
    resumed = "resume" in sys.argv[1:]
    emit({
        "type": "thread.started",
        "thread_id": "codex-resumed" if resumed else "codex-thread-123",
    })
    emit({"type": "turn.started"})
    emit({
        "type": "item.completed",
        "item": {"id": "item-1", "type": "agent_message", "text": "done"},
    })
    emit({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 10,
            "cached_input_tokens": 3,
            "output_tokens": 20,
            "reasoning_output_tokens": 4,
        },
    })
