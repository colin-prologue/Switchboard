"""Tests for the workspace manager.

implements: core §9 (Workspace Management and Safety), §17.2 (test matrix)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from orchestrator.types import HookError, HooksConfig, WorkspaceError
from orchestrator.workspace import WorkspaceManager


def hooks(
    *,
    after_create: str | None = None,
    before_run: str | None = None,
    after_run: str | None = None,
    before_remove: str | None = None,
    timeout_ms: int = 5000,
) -> HooksConfig:
    return HooksConfig(
        after_create=after_create,
        before_run=before_run,
        after_run=after_run,
        before_remove=before_remove,
        timeout_ms=timeout_ms,
    )


def no_op_manager(tmp_path: Path) -> WorkspaceManager:
    return WorkspaceManager(root=tmp_path, hooks=hooks())


# --- path determinism / sanitization / containment --------------------------


def test_path_for_is_deterministic(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    p1 = mgr.path_for("42")
    p2 = mgr.path_for("42")
    assert p1 == p2
    assert p1.parent == mgr.root


@pytest.mark.parametrize(
    "identifier",
    ["../evil", "a/b#c", "../../etc/passwd", "foo/../../bar"],
)
def test_path_for_sanitizes_dangerous_identifiers(tmp_path: Path, identifier: str) -> None:
    # sanitize_workspace_key only maps `/` (and other disallowed chars) to `_`;
    # "." and "-" survive as literal characters, so these keys land as literal
    # (harmless) filenames one level directly under root.
    mgr = no_op_manager(tmp_path)
    path = mgr.path_for(identifier)
    resolved_root = tmp_path.resolve()
    assert path.parent == resolved_root


def test_path_for_rejects_identifier_that_sanitizes_to_dotdot(tmp_path: Path) -> None:
    # sanitize_workspace_key leaves "." and "-" untouched, so an identifier of
    # exactly ".." sanitizes to the literal string "..", which would resolve
    # outside root. Containment enforcement (core §9.5 invariant 2) must catch
    # this rather than silently escaping root.
    mgr = no_op_manager(tmp_path)
    with pytest.raises(WorkspaceError):
        mgr.path_for("..")


def test_path_for_enforces_containment(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    # A leading slash is not special to sanitize_workspace_key (it maps to "_"),
    # so this still sanitizes to a safe key inside root.
    path = mgr.path_for("/etc/passwd")
    assert tmp_path.resolve() in path.parents or path == tmp_path.resolve()


# --- creation / reuse --------------------------------------------------------


async def test_missing_dir_is_created(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    ws = await mgr.create_for_issue("101")
    assert ws.created_now is True
    assert ws.path.is_dir()


async def test_existing_dir_is_reused(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    ws1 = await mgr.create_for_issue("101")
    assert ws1.created_now is True
    ws2 = await mgr.create_for_issue("101")
    assert ws2.created_now is False
    assert ws2.path == ws1.path


async def test_after_create_runs_only_on_creation(tmp_path: Path) -> None:
    marker = tmp_path / "marker.log"
    mgr = WorkspaceManager(root=tmp_path / "root", hooks=hooks(after_create=f'echo hit >> "{marker}"'))
    await mgr.create_for_issue("55")
    await mgr.create_for_issue("55")  # second call: reused, hook must not re-run
    assert marker.read_text().splitlines() == ["hit"]


async def test_non_directory_collision_raises(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    path = mgr.path_for("blocked")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("i am a file, not a dir")

    with pytest.raises(WorkspaceError):
        await mgr.create_for_issue("blocked")


async def test_after_create_failure_raises_and_removes_dir(tmp_path: Path) -> None:
    mgr = WorkspaceManager(root=tmp_path, hooks=hooks(after_create="exit 1"))
    with pytest.raises(WorkspaceError):
        await mgr.create_for_issue("77")
    assert not (tmp_path / "77").exists()


async def test_after_create_timeout_raises_removes_dir_and_returns_promptly(tmp_path: Path) -> None:
    mgr = WorkspaceManager(root=tmp_path, hooks=hooks(after_create="sleep 30", timeout_ms=200))
    start = time.monotonic()
    with pytest.raises(WorkspaceError):
        await mgr.create_for_issue("88")
    elapsed = time.monotonic() - start
    assert elapsed < 2.0
    assert not (tmp_path / "88").exists()


async def test_hooks_none_spawns_no_subprocess(tmp_path: Path) -> None:
    marker = tmp_path / "marker.log"
    mgr = WorkspaceManager(root=tmp_path / "root", hooks=hooks())  # all hooks None
    ws = await mgr.create_for_issue("99")
    assert ws.created_now is True
    assert not marker.exists()


# --- before_run / after_run ---------------------------------------------------


async def test_before_run_failure_raises_hook_error(tmp_path: Path) -> None:
    mgr = WorkspaceManager(root=tmp_path, hooks=hooks(before_run="exit 1"))
    ws = await mgr.create_for_issue("1")
    with pytest.raises(HookError):
        await mgr.run_before_run(ws)


async def test_before_run_success_has_extra_env_and_correct_cwd(tmp_path: Path) -> None:
    marker = tmp_path.parent / "before_run_marker.txt"
    script = f'test "$SB_TEST_VAR" = "hello" && test "$PWD" = "$(pwd -P)" && pwd > "{marker}"'
    mgr = WorkspaceManager(
        root=tmp_path,
        hooks=hooks(before_run=script),
        extra_env={"SB_TEST_VAR": "hello"},
    )
    ws = await mgr.create_for_issue("2")
    await mgr.run_before_run(ws)
    assert marker.read_text().strip() == str(ws.path.resolve())


async def test_after_run_failure_is_ignored(tmp_path: Path) -> None:
    mgr = WorkspaceManager(root=tmp_path, hooks=hooks(after_run="exit 1"))
    ws = await mgr.create_for_issue("3")
    # Must not raise.
    await mgr.run_after_run(ws)


# --- removal ------------------------------------------------------------------


async def test_before_remove_failure_ignored_and_dir_still_removed(tmp_path: Path) -> None:
    mgr = WorkspaceManager(root=tmp_path, hooks=hooks(before_remove="exit 1"))
    ws = await mgr.create_for_issue("4")
    assert ws.path.is_dir()
    await mgr.remove_for_issue("4")  # must not raise
    assert not ws.path.exists()


async def test_remove_nonexistent_is_noop(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    await mgr.remove_for_issue("does-not-exist")  # must not raise


async def test_cleanup_terminal_swallows_errors(tmp_path: Path) -> None:
    mgr = no_op_manager(tmp_path)
    await mgr.create_for_issue("5")
    await mgr.create_for_issue("6")
    # Includes a nonexistent identifier too; should not raise for any of them.
    await mgr.cleanup_terminal(["5", "6", "does-not-exist"])
    assert not (tmp_path / "5").exists()
    assert not (tmp_path / "6").exists()
