"""Claude CLI execution adapter (Agent Runner).

implements: core §10 (Agent Runner Protocol) / overridden by: SPEC.md §1
(Claude CLI binding: `claude -p --output-format stream-json`, session_id from
`system/init`, continuation via `--resume`, terminal `result.subtype` mapping,
`--max-turns` / `--max-budget-usd` passthrough)

Launch contract (core §10.1, SPEC.md §1): the coding-agent process is started
via `bash -lc <command>` with cwd fixed to the per-issue workspace (safety
invariant 1, core §9.5) and a 10 MB max protocol-line size. Approval/sandbox
posture (core §10.5) is delegated entirely to the configured Claude CLI
command (non-interactive permission mode + PreToolUse hooks are the caller's
responsibility, per SPEC.md §1); this module has no approval logic of its own
and treats any non-"success" terminal result as a failed attempt, which is
this implementation's documented equivalent of "user-input-required = hard
failure" (core §10.5, §10.6).

stdlib only.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import signal
from datetime import datetime, timezone
from pathlib import Path

from .types import AgentEvent, ClaudeConfig, EventCallback, TurnResult

MAX_LINE_BYTES = 10 * 1024 * 1024  # core §10.1
STDERR_TAIL_CHARS = 500
NOTIFICATION_TEXT_CHARS = 200

GUARD_PATH = Path(__file__).with_name("guard.py")
GUARD_MATCHER = "Write|Edit|MultiEdit|NotebookEdit"


def _write_guard_settings(workspace: Path) -> Path:
    """Materialize the PreToolUse containment-guard settings (SPEC.md §1
    "sandbox/safety invariants -> PreToolUse hooks vetoing tool calls outside
    the per-issue workspace"). The file lives NEXT TO the workspace — never
    inside it — so the clone stays clean and the agent cannot commit it.
    """
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": GUARD_MATCHER,
                    "hooks": [
                        {
                            # -I (isolated): keeps the script's directory off
                            # sys.path, where our types.py would shadow the
                            # stdlib `types` module.
                            "type": "command",
                            "command": f"python3 -I {shlex.quote(str(GUARD_PATH))}",
                        }
                    ],
                }
            ]
        }
    }
    path = workspace.parent / f".{workspace.name}.claude-settings.json"
    path.write_text(json.dumps(settings))
    return path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stderr_tail(chunks: list[bytes]) -> str:
    raw = b"".join(chunks).decode("utf-8", errors="replace")
    return raw[-STDERR_TAIL_CHARS:]


def _summarize_message(msg: dict) -> dict:
    """Build a short, non-buffering summary payload for assistant/user/other
    stream-json message lines (core §10.4 `notification` / `other_message`)."""
    msg_type = msg.get("type", "unknown")
    text = ""
    inner = msg.get("message")
    if isinstance(inner, dict):
        content = inner.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        parts.append(block["text"])
                    elif block.get("type") == "tool_use":
                        parts.append(f"[tool_use:{block.get('name', '?')}]")
                    elif block.get("type") == "tool_result":
                        parts.append("[tool_result]")
            text = " ".join(parts)
    return {"type": msg_type, "text": text[:NOTIFICATION_TEXT_CHARS]}


class ClaudeRunner:
    """Wraps one `claude -p` subprocess invocation as a single logical turn.

    implements: core §10.7 (Agent Runner Contract), steps 3-5 (start session,
    forward events, fail attempt on error). Workspace creation/reuse and
    prompt rendering are the caller's responsibility (core §10.7 steps 1-2).
    """

    def __init__(self, cfg: ClaudeConfig) -> None:
        self.cfg = cfg

    @staticmethod
    def _build_env(agent_token: str | None) -> dict[str, str] | None:
        """Subprocess env for one turn. With a token (issue #10), overlay it as
        both GITHUB_TOKEN and GH_TOKEN over the inherited env — the agent's `gh`
        calls and its `git push` (via the before_run credential helper) then act
        as the bot. The token is per-turn, so a session spanning the hourly
        expiry picks up a fresh mint on its next turn. None -> inherit as-is."""
        if agent_token is None:
            return None
        env = dict(os.environ)
        env["GITHUB_TOKEN"] = agent_token
        env["GH_TOKEN"] = agent_token
        return env

    def _build_command(self, resume_session_id: str | None,
                       settings_path: Path | None = None) -> str:
        cmd = self.cfg.command
        cmd += f" --max-turns {self.cfg.max_turns}"
        if self.cfg.max_budget_usd is not None:
            cmd += f" --max-budget-usd {self.cfg.max_budget_usd}"
        if resume_session_id:
            cmd += f" --resume {shlex.quote(resume_session_id)}"
        if settings_path is not None:
            cmd += f" --settings {shlex.quote(str(settings_path))}"
        return cmd

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
            raise ValueError(f"workspace does not exist or is not a directory: {workspace}")

        settings_path = _write_guard_settings(workspace)
        command = self._build_command(resume_session_id, settings_path)
        env = self._build_env(agent_token)

        def emit(event: str, payload: dict, pid: int | None, usage: dict | None = None) -> None:
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
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                command,
                cwd=str(workspace),
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE_BYTES,
                start_new_session=True,  # own process group -> killpg on timeout
            )
        except (OSError, FileNotFoundError) as exc:
            emit("startup_failed", {"error": str(exc), "command": command}, None)
            return TurnResult(status="failed", session_id=None, error="claude_not_found")

        pid = proc.pid
        stderr_chunks: list[bytes] = []

        async def drain_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(drain_stderr())

        # Write prompt to stdin and close it.
        try:
            assert proc.stdin is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # process may have already exited (e.g. exec failure surfaced via exit code)

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
            # A process still alive here is being abandoned (result already
            # decided, or an exception is unwinding) — it must not outlive the
            # turn, so escalate to SIGKILL rather than leaving a zombie agent
            # holding the workspace while a retry dispatches a second one.
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
            settings_path.unlink(missing_ok=True)  # guard sidecar is per-turn

        loop = asyncio.get_event_loop()
        turn_deadline = loop.time() + self.cfg.turn_timeout_ms / 1000.0

        assert proc.stdout is not None
        first_line = True
        session_id: str | None = None
        result: TurnResult | None = None

        try:
            while True:
                remaining_turn = turn_deadline - loop.time()
                if remaining_turn <= 0:
                    # Deadline expired between reads: same handling as the
                    # in-read timeout below — kill the agent and fail the turn
                    # (raising here would abandon a live subprocess).
                    await kill_process_group()
                    await reap()
                    emit(
                        "turn_failed",
                        {"error": "turn_timeout", "stderr": _stderr_tail(stderr_chunks)},
                        pid,
                    )
                    return TurnResult(status="timed_out", session_id=session_id, error="turn_timeout")

                if first_line:
                    read_timeout = min(self.cfg.read_timeout_ms / 1000.0, remaining_turn)
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=read_timeout)
                    except asyncio.TimeoutError:
                        await kill_process_group()
                        await reap()
                        emit(
                            "startup_failed",
                            {"error": "no protocol output before read_timeout_ms", "stderr": _stderr_tail(stderr_chunks)},
                            pid,
                        )
                        return TurnResult(status="failed", session_id=None, error="response_timeout")
                else:
                    try:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=remaining_turn)
                    except asyncio.TimeoutError:
                        await kill_process_group()
                        await reap()
                        emit(
                            "turn_failed",
                            {"error": "turn_timeout", "stderr": _stderr_tail(stderr_chunks)},
                            pid,
                        )
                        return TurnResult(status="timed_out", session_id=session_id, error="turn_timeout")

                if not line:
                    # EOF: process exited without further output.
                    break

                first_line = False
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue

                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    emit("malformed", {"line": raw[:NOTIFICATION_TEXT_CHARS]}, pid)
                    continue

                msg_type = msg.get("type")

                if msg_type == "system" and msg.get("subtype") == "init":
                    session_id = msg.get("session_id")
                    emit("session_started", {"session_id": session_id}, pid)
                    continue

                if msg_type == "result":
                    session_id = msg.get("session_id", session_id)
                    subtype = msg.get("subtype")
                    cost_usd = msg.get("total_cost_usd", 0.0) or 0.0
                    usage = msg.get("usage") or {}
                    num_turns = msg.get("num_turns", 0) or 0
                    denials = msg.get("permission_denials") or []

                    if subtype == "success":
                        emit(
                            "turn_completed",
                            {
                                "subtype": subtype,
                                "total_cost_usd": cost_usd,
                                "num_turns": num_turns,
                                "permission_denials": denials,
                            },
                            pid,
                            usage=usage,
                        )
                        result = TurnResult(
                            status="succeeded",
                            session_id=session_id,
                            cost_usd=cost_usd,
                            usage=usage,
                            num_turns=num_turns,
                        )
                    else:
                        emit(
                            "turn_failed",
                            {
                                "subtype": subtype,
                                "total_cost_usd": cost_usd,
                                "num_turns": num_turns,
                                "permission_denials": denials,
                            },
                            pid,
                            usage=usage,
                        )
                        result = TurnResult(
                            status="failed",
                            session_id=session_id,
                            error=subtype,
                            cost_usd=cost_usd,
                            usage=usage,
                            num_turns=num_turns,
                        )
                    break

                # assistant / user / any other message type: short summary only.
                emit("notification", _summarize_message(msg), pid)

        except asyncio.CancelledError:
            # Worker task cancelled from outside (stall/reconciliation/shutdown,
            # core §8.5): the agent subprocess must die with the worker.
            await kill_process_group()
            raise
        finally:
            await reap()

        if result is not None:
            return result

        # Process exited (or stdout closed) without ever emitting a result line.
        # bash exit 127 ("command not found") / 126 ("not executable") means the
        # configured cfg.command never launched the agent at all (core §10.6
        # codex_not_found, rebound to claude_not_found per SPEC.md §1).
        if proc.returncode in (126, 127) and first_line:
            emit(
                "startup_failed",
                {"error": "bash launch failure", "returncode": proc.returncode, "stderr": _stderr_tail(stderr_chunks)},
                pid,
            )
            return TurnResult(status="failed", session_id=None, error="claude_not_found")

        emit("turn_failed", {"error": "port_exit", "stderr": _stderr_tail(stderr_chunks)}, pid)
        return TurnResult(status="failed", session_id=session_id, error="port_exit")
