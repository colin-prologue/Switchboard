"""Workspace manager.

implements: core §9 (Workspace Management and Safety), §15.2/§15.4 (safety
and hook script safety), §17.2 (test matrix)

Owns per-issue workspace directories under a configured root, and the
lifecycle hooks (`after_create`, `before_run`, `after_run`, `before_remove`)
that wrap directory creation, run attempts, and cleanup. Enforces the two
core safety invariants: sanitized workspace directory names (core §9.5
invariant 3) and root containment (core §9.5 invariant 2).

Keep this module stdlib only.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
from pathlib import Path

from orchestrator.log import log
from orchestrator.types import HookError, HooksConfig, Workspace, WorkspaceError, sanitize_workspace_key

_LOG_TRUNCATE = 1000  # core §15.4: hook output SHOULD be truncated in logs


class WorkspaceManager:
    """Creates, prepares, and removes per-issue workspace directories."""

    def __init__(self, root: Path, hooks: HooksConfig) -> None:
        self.root = Path(root).resolve()
        self.hooks = hooks

    # --- path safety (core §9.5) ---------------------------------------------

    def path_for(self, identifier: str) -> Path:
        """Compute the sanitized, containment-checked workspace path for `identifier`."""
        key = sanitize_workspace_key(identifier)
        candidate = (self.root / key).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise WorkspaceError(f"workspace path {candidate} escapes root {self.root}")
        return candidate

    # --- creation/reuse (core §9.2) -------------------------------------------

    async def create_for_issue(self, identifier: str) -> Workspace:
        """Ensure the workspace directory for `identifier` exists; run after_create if newly created."""
        key = sanitize_workspace_key(identifier)
        path = self.path_for(identifier)

        if path.exists() and not path.is_dir():
            raise WorkspaceError(f"workspace path {path} exists and is not a directory")

        created_now = not path.exists()
        path.mkdir(parents=True, exist_ok=True)

        if created_now and self.hooks.after_create is not None:
            try:
                await self._run_hook(self.hooks.after_create, path, "after_create", identifier)
            except HookError as exc:
                # core §9.3: MAY remove the partially-prepared dir on after_create failure.
                shutil.rmtree(path, ignore_errors=True)
                raise WorkspaceError(f"after_create hook failed for issue {identifier}: {exc}") from exc

        return Workspace(path=path, workspace_key=key, created_now=created_now)

    # --- run-attempt hooks (core §9.4) ----------------------------------------

    async def run_before_run(self, ws: Workspace) -> None:
        """Run before_run; failure/timeout is fatal to the current run attempt."""
        if self.hooks.before_run is None:
            return
        await self._run_hook(self.hooks.before_run, ws.path, "before_run", ws.workspace_key)

    async def run_after_run(self, ws: Workspace) -> None:
        """Run after_run; failure/timeout is logged and ignored."""
        if self.hooks.after_run is None:
            return
        try:
            await self._run_hook(self.hooks.after_run, ws.path, "after_run", ws.workspace_key)
        except HookError as exc:
            log("hook.after_run.ignored", issue_identifier=ws.workspace_key, error=str(exc))

    # --- removal (core §8.6, §9.4) --------------------------------------------

    async def remove_for_issue(self, identifier: str) -> None:
        """Remove the workspace for `identifier`, if present. No-op if absent."""
        path = self.path_for(identifier)
        if not path.exists():
            return

        if self.hooks.before_remove is not None:
            try:
                await self._run_hook(self.hooks.before_remove, path, "before_remove", identifier)
            except HookError as exc:
                log("hook.before_remove.ignored", issue_identifier=identifier, error=str(exc))

        shutil.rmtree(path, ignore_errors=True)

    async def cleanup_terminal(self, identifiers: list[str]) -> None:
        """Remove workspaces for terminal issues (core §8.6 startup sweep); swallow errors."""
        for identifier in identifiers:
            try:
                await self.remove_for_issue(identifier)
            except Exception as exc:  # noqa: BLE001 - best-effort sweep, never propagate
                log("workspace.cleanup_terminal.error", issue_identifier=identifier, error=str(exc))

    # --- hook execution (core §9.4, §15.4) ------------------------------------

    async def _run_hook(self, script: str, cwd: Path, hook_name: str, identifier: str) -> None:
        env = dict(os.environ)
        timeout_s = self.hooks.timeout_ms / 1000.0

        log("hook.start", hook=hook_name, issue_identifier=identifier, cwd=str(cwd))

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-lc",
            script,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            await self._kill_process_group(proc)
            log("hook.timeout", hook=hook_name, issue_identifier=identifier, timeout_ms=self.hooks.timeout_ms)
            raise HookError(f"{hook_name} hook timed out after {self.hooks.timeout_ms}ms")
        except asyncio.CancelledError:
            # hard-cancelled mid-hook (e.g. shutdown teardown grace expired):
            # the subprocess must not outlive the await that supervises it
            await self._kill_process_group(proc)
            log("hook.cancelled", hook=hook_name, issue_identifier=identifier)
            raise

        if proc.returncode != 0:
            log(
                "hook.failed",
                hook=hook_name,
                issue_identifier=identifier,
                returncode=proc.returncode,
                stdout=_truncate(stdout),
                stderr=_truncate(stderr),
            )
            raise HookError(f"{hook_name} hook exited {proc.returncode}")

    async def _kill_process_group(self, proc: asyncio.subprocess.Process) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:  # noqa: BLE001 - best-effort reap after kill
            pass


def _truncate(data: bytes) -> str:
    text = data.decode(errors="replace")
    if len(text) > _LOG_TRUNCATE:
        text = text[:_LOG_TRUNCATE] + "…"
    return text
