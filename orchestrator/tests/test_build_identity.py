"""Startup build identity — the sha/dirty fields on the startup record (#143).

Two layers under test, and they are separate on purpose:

* `resolve_build_identity` — the resolution order (env var, then the MODULE'S
  tree, then `"unknown"`), exercised against real git repos rather than path
  fakes, because "which tree did it ask?" is the whole question.
* `Orchestrator.run()` — that the identity actually reaches the existing
  `"orchestrator starting"` record without displacing anything already on it.
  Before this ticket no test greped that record at all.

The autouse fixture in `conftest.py` unbinds `SB_LAUNCH_SHA` suite-wide (a
dispatched worker inherits a bound one from `run-project.sh`), so every test
here starts from the unbound direction and binds it explicitly when that is
what it means to test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator import build_identity
from orchestrator.build_identity import MODULE_TREE, UNKNOWN, resolve_build_identity
from orchestrator.scheduler import Orchestrator

LAUNCH_SHA = "0" * 40


def _git(tree: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(tree), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _seed_repo(path: Path, *, message: str = "seed") -> Path:
    """A real one-commit repo. Distinct messages give distinct shas, which is
    how the module-tree-vs-cwd test tells the two apart."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", message)
    return path


# --- resolution order ---------------------------------------------------------


def test_launch_sha_wins_over_the_module_tree(tmp_path, monkeypatch) -> None:
    """`SB_LAUNCH_SHA` is captured before the process starts, so it stays right
    even after the tree moves underneath a running orchestrator — which is the
    only reason it outranks a live `rev-parse`."""
    tree = _seed_repo(tmp_path / "tree")
    monkeypatch.setenv("SB_LAUNCH_SHA", LAUNCH_SHA)

    sha, dirty = resolve_build_identity(tree=tree)

    assert sha == LAUNCH_SHA
    assert sha != _git(tree, "rev-parse", "HEAD")
    assert dirty == "false"  # dirty is ALWAYS the tree's; the env var has none


def test_empty_launch_sha_falls_through_to_the_tree(tmp_path, monkeypatch) -> None:
    """`SB_LAUNCH_SHA=""` — what a `set -a` over a blank assignment yields — is
    bound but identifies nothing. Falsiness, not membership, is the test."""
    tree = _seed_repo(tmp_path / "tree")
    monkeypatch.setenv("SB_LAUNCH_SHA", "")

    sha, _ = resolve_build_identity(tree=tree)

    assert sha == _git(tree, "rev-parse", "HEAD")


def test_fallback_sha_is_the_module_tree_not_the_cwd(tmp_path, monkeypatch) -> None:
    """The launchd path starts from an arbitrary directory, so a cwd-derived
    sha would answer a different question than "which build is this". Run from
    inside a foreign repo and the answer must still be this module's tree."""
    foreign = _seed_repo(tmp_path / "foreign", message="foreign")
    monkeypatch.chdir(foreign)
    try:
        expected = _git(MODULE_TREE, "rev-parse", "HEAD")
    except subprocess.CalledProcessError:
        pytest.skip("module tree is not a git work tree (non-editable install)")

    sha, _ = resolve_build_identity(env={})

    assert sha == expected
    assert sha != _git(foreign, "rev-parse", "HEAD")


def test_no_git_metadata_yields_unknown_rather_than_raising(tmp_path) -> None:
    """No `.git` and no `SB_LAUNCH_SHA` — an exported copy. Both fields say so
    explicitly; neither resolution raises."""
    bare = tmp_path / "not-a-repo"
    bare.mkdir()

    assert resolve_build_identity(env={}, tree=bare) == (UNKNOWN, UNKNOWN)


def test_dirty_reflects_uncommitted_changes(tmp_path) -> None:
    """The reason `dirty` is worth a field: a tree that matches a sha and a
    tree that merely started from one are different builds."""
    tree = _seed_repo(tmp_path / "tree")
    assert resolve_build_identity(env={}, tree=tree)[1] == "false"

    (tree / "scratch.txt").write_text("uncommitted\n")

    assert resolve_build_identity(env={}, tree=tree)[1] == "true"


# --- the startup record --------------------------------------------------------

WORKFLOW_TMPL = """---
tracker:
  kind: github
  repo: "acme/api"
  api_key: "test-token"
  active_states: ["todo", "in progress"]
  terminal_states: ["closed"]
polling:
  interval_ms: 100
workspace:
  root: "{ws_root}"
agent:
  max_concurrent_agents: 1
  max_turns: 1
  max_retry_backoff_ms: 500
  max_sessions_per_issue: 2
claude:
  command: "unused-by-fake-tracker"
  max_turns: 1
  turn_timeout_ms: 5000
  read_timeout_ms: 3000
  stall_timeout_ms: 0
---
prompt body
"""


class _FakeTracker:
    """Enough tracker for the two startup sweeps to be no-ops. `run()` must
    reach its loop guard without a network call."""

    async def fetch_issues_by_states(self, state_names):
        return []

    async def fetch_candidate_issues(self):
        return []


async def _startup_stderr(tmp_path: Path, capsys) -> str:
    """Drive `run()` through startup exactly once and return what it logged.

    `_stopping` is pre-set so the poll loop's guard is false on first look: the
    startup record is emitted before the loop, so nothing here needs a tick.
    """
    ws_root = tmp_path / "ws"
    workflow = tmp_path / "WORKFLOW.md"
    workflow.write_text(WORKFLOW_TMPL.format(ws_root=ws_root))

    orch = Orchestrator(workflow)
    real_components = orch._components
    orch._load_workflow(initial=True)
    orch._components = lambda: (_FakeTracker(), real_components()[1])
    orch._stopping = True

    await orch.run()
    return capsys.readouterr().err


async def test_startup_record_carries_sha_and_dirty(tmp_path, monkeypatch, capsys):
    """The happy path: the existing record gains both fields and loses none."""
    monkeypatch.setattr(build_identity, "MODULE_TREE", _seed_repo(tmp_path / "src"))
    monkeypatch.setenv("SB_LAUNCH_SHA", LAUNCH_SHA)

    line = next(
        ln for ln in (await _startup_stderr(tmp_path, capsys)).splitlines()
        if "orchestrator starting" in ln
    )

    assert f"sha={LAUNCH_SHA}" in line
    assert "dirty=false" in line
    # The record is load-bearing (SETUP.md:293 anchors tracebacks to it): the
    # name and every pre-existing field survive the extension.
    assert "workflow=" in line
    assert "repo=acme/api" in line
    assert f"workspace_root={tmp_path / 'ws'}" in line


async def test_startup_survives_unresolvable_git_metadata(tmp_path, monkeypatch, capsys):
    """No `SB_LAUNCH_SHA`, no work tree: startup still completes and the record
    is still emitted, saying `unknown` in both columns rather than nothing."""
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    monkeypatch.setattr(build_identity, "MODULE_TREE", nowhere)

    line = next(
        ln for ln in (await _startup_stderr(tmp_path, capsys)).splitlines()
        if "orchestrator starting" in ln
    )

    assert "sha=unknown" in line
    assert "dirty=unknown" in line
