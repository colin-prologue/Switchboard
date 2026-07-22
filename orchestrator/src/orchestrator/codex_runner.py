"""Standalone Codex CLI execution adapter.

Stage 4 deliberately leaves this adapter out of workflow parsing and the
production runner selector. It normalizes `codex exec --json` JSONL into the
provider-neutral AgentRunner contract.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from .failure_classification import classify_codex_failure
from .types import AgentEvent, CodexConfig, EventCallback, FailureClass, TurnResult


MAX_LINE_BYTES = 10 * 1024 * 1024
STDERR_TAIL_CHARS = 500
NOTIFICATION_TEXT_CHARS = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stderr_tail(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]


def _error_text(message: dict) -> str:
    error = message.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:NOTIFICATION_TEXT_CHARS]
    if isinstance(error, str):
        return error[:NOTIFICATION_TEXT_CHARS]
    if isinstance(message.get("message"), str):
        return message["message"][:NOTIFICATION_TEXT_CHARS]
    return ""


def _error_code(message: dict) -> str | None:
    error = message.get("error")
    if isinstance(error, dict) and isinstance(error.get("code"), str):
        return error["code"]
    if isinstance(message.get("code"), str):
        return message["code"]
    return None


def _notification(message: dict) -> dict:
    payload = {"type": message.get("type", "unknown")}
    item = message.get("item")
    if isinstance(item, dict):
        payload["item_type"] = item.get("type", "unknown")
        text = item.get("text")
        if not isinstance(text, str):
            text = item.get("command") if isinstance(item.get("command"), str) else ""
        payload["text"] = text[:NOTIFICATION_TEXT_CHARS]
    return payload


def _open_transcript(workspace: Path, pid: int) -> BinaryIO | None:
    """Open a local raw-JSONL transcript without making it git-visible.

    Codex emits the ground-truth stream on stdout, so capture it while parsing
    rather than relying on a provider-specific on-disk session layout. This is
    best effort: an unavailable transcript path must never change turn outcome.
    """
    exclude = workspace / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ".run/" not in existing.splitlines():
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(".run/\n")

    directory = workspace / ".run" / "transcripts"
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return (directory / f"codex-{timestamp}-{pid}.jsonl").open("ab")


def _write_transcript(transcript: BinaryIO | None, line: bytes) -> BinaryIO | None:
    """Append one raw output line, disabling only capture if storage fails."""
    if transcript is None:
        return None
    try:
        transcript.write(line)
        transcript.flush()
        return transcript
    except OSError:
        try:
            transcript.close()
        except OSError:
            pass
        return None


class CodexRunner:
    """Run one Codex CLI turn with explicit headless safety settings."""

    provider_id = "codex"

    def __init__(self, cfg: CodexConfig) -> None:
        self.cfg = cfg
        self.turn_timeout_ms = cfg.turn_timeout_ms
        self.stall_timeout_ms = cfg.stall_timeout_ms
        self.max_budget_usd: float | None = None

    def _build_argv(self, resume_session_id: str | None) -> list[str]:
        argv = shlex.split(self.cfg.command)
        if not argv:
            raise ValueError("codex command must not be empty")
        if resume_session_id:
            return [
                *argv,
                "exec",
                "resume",
                "--ignore-user-config",
                "--json",
                resume_session_id,
                "-",
            ]
        return [
            *argv,
            "exec",
            "--ignore-user-config",
            "--color",
            "never",
            "--json",
            "-",
        ]

    @staticmethod
    def _build_env(agent_token: str | None) -> dict[str, str]:
        env = dict(os.environ)
        # Stage 4 is subscription-only. Inline API keys override saved account
        # auth for `codex exec`, so keep them out of the child process.
        env.pop("CODEX_API_KEY", None)
        env.pop("OPENAI_API_KEY", None)
        env["NO_COLOR"] = "1"
        if agent_token is not None:
            env["GITHUB_TOKEN"] = agent_token
            env["GH_TOKEN"] = agent_token
        return env

    async def run_turn(
        self,
        workspace: Path,
        prompt: str,
        resume_session_id: str | None,
        on_event: EventCallback,
        issue_id: str,
        agent_token: str | None = None,
    ) -> TurnResult:
        if not workspace.is_dir():
            raise ValueError(
                f"workspace does not exist or is not a directory: {workspace}"
            )

        def emit(
            event: str,
            payload: dict,
            pid: int | None,
            usage: dict | None = None,
        ) -> None:
            on_event(
                issue_id,
                AgentEvent(
                    event=event,
                    timestamp=_now(),
                    pid=pid,
                    usage=usage,
                    payload=payload,
                ),
            )

        try:
            argv = self._build_argv(resume_session_id)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(workspace),
                env=self._build_env(agent_token),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,
                start_new_session=True,
            )
        except (OSError, ValueError) as exc:
            emit("startup_failed", {"error": str(exc)}, None)
            return TurnResult(
                status="failed",
                session_id=None,
                error="codex_not_found",
                failure_class=FailureClass.RUNNER_STARTUP,
            )

        pid = proc.pid
        stderr_chunks: list[bytes] = []
        try:
            transcript = _open_transcript(workspace, pid)
        except OSError:
            transcript = None

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            while chunk := await proc.stderr.read(4096):
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(drain_stderr())

        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass

        async def kill_process_group() -> None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

        async def reap() -> None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                await kill_process_group()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self.cfg.turn_timeout_ms / 1000
        first_line = True
        session_id: str | None = None
        result: TurnResult | None = None

        assert proc.stdout is not None
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await kill_process_group()
                    emit(
                        "turn_failed",
                        {"error": "turn_timeout", "stderr": _stderr_tail(stderr_chunks)},
                        pid,
                    )
                    return TurnResult(
                        status="timed_out",
                        session_id=session_id,
                        error="turn_timeout",
                        failure_class=FailureClass.RUNNER_TIMEOUT,
                    )

                timeout = remaining
                if first_line:
                    timeout = min(self.cfg.read_timeout_ms / 1000, remaining)
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                except asyncio.TimeoutError:
                    await kill_process_group()
                    if first_line:
                        emit(
                            "startup_failed",
                            {
                                "error": "no protocol output before read_timeout_ms",
                                "stderr": _stderr_tail(stderr_chunks),
                            },
                            pid,
                        )
                        return TurnResult(
                            status="failed",
                            session_id=None,
                            error="response_timeout",
                            failure_class=FailureClass.RUNNER_TIMEOUT,
                        )
                    emit(
                        "turn_failed",
                        {"error": "turn_timeout", "stderr": _stderr_tail(stderr_chunks)},
                        pid,
                    )
                    return TurnResult(
                        status="timed_out",
                        session_id=session_id,
                        error="turn_timeout",
                        failure_class=FailureClass.RUNNER_TIMEOUT,
                    )

                if not line:
                    break
                transcript = _write_transcript(transcript, line)
                first_line = False
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    emit("malformed", {"line": raw[:NOTIFICATION_TEXT_CHARS]}, pid)
                    continue
                if not isinstance(message, dict):
                    emit("malformed", {"line": raw[:NOTIFICATION_TEXT_CHARS]}, pid)
                    continue

                message_type = message.get("type")
                if message_type == "thread.started":
                    candidate = message.get("thread_id")
                    if isinstance(candidate, str) and candidate:
                        session_id = candidate
                        emit("session_started", {"session_id": session_id}, pid)
                    else:
                        emit("malformed", {"line": raw[:NOTIFICATION_TEXT_CHARS]}, pid)
                    continue

                if message_type == "turn.completed":
                    if session_id is None:
                        emit("turn_failed", {"error": "missing_session_id"}, pid)
                        result = TurnResult(
                            status="failed",
                            session_id=None,
                            error="missing_session_id",
                            failure_class=FailureClass.RUNNER_PROTOCOL,
                        )
                    else:
                        usage = message.get("usage")
                        if not isinstance(usage, dict):
                            usage = {}
                        emit("turn_completed", {}, pid, usage=usage)
                        result = TurnResult(
                            status="succeeded",
                            session_id=session_id,
                            usage=usage,
                            num_turns=1,
                        )
                    break

                if message_type == "turn.failed":
                    detail = _error_text(message)
                    emit(
                        "turn_failed",
                        {"error": detail},
                        pid,
                    )
                    result = TurnResult(
                        status="failed",
                        session_id=session_id,
                        error="codex_turn_failed",
                        failure_class=classify_codex_failure(
                            code=_error_code(message), detail=detail
                        ),
                    )
                    break

                if message_type == "error":
                    detail = _error_text(message)
                    emit(
                        "turn_failed",
                        {"error": detail},
                        pid,
                    )
                    result = TurnResult(
                        status="failed",
                        session_id=session_id,
                        error="codex_error",
                        failure_class=classify_codex_failure(
                            code=_error_code(message), detail=detail
                        ),
                    )
                    break

                emit("notification", _notification(message), pid)

        except asyncio.CancelledError:
            await kill_process_group()
            raise
        finally:
            await reap()
            if transcript is not None:
                try:
                    transcript.close()
                except OSError:
                    pass

        if result is not None:
            return result

        emit(
            "turn_failed",
            {"error": "port_exit", "stderr": _stderr_tail(stderr_chunks)},
            pid,
        )
        return TurnResult(
            status="failed",
            session_id=session_id,
            error="port_exit",
            failure_class=FailureClass.RUNNER_PROTOCOL,
        )
