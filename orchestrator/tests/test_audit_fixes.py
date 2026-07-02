"""Regression tests for the Phase-1 conformance-audit findings."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.runner import ClaudeRunner, _write_guard_settings, GUARD_PATH
from orchestrator.types import ClaudeConfig, WorkflowDefinition, WorkflowError, sanitize_workspace_key
from orchestrator.workflow import Config


# --- finding 3: embedded $VAR expansion in path fields -------------------------

def _cfg(config: dict, tmp_path: Path) -> Config:
    return Config(WorkflowDefinition(config=config, prompt_template="x"), tmp_path)


def test_workspace_root_embedded_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("SB_TEST_BASE", str(tmp_path / "base"))
    cfg = _cfg({"workspace": {"root": "$SB_TEST_BASE/workspaces"}}, tmp_path)
    assert cfg.workspace_root() == tmp_path / "base" / "workspaces"


def test_workspace_root_unresolved_var_is_error_not_workflow_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("SB_NO_SUCH_VAR", raising=False)
    cfg = _cfg({"workspace": {"root": "$SB_NO_SUCH_VAR/workspaces"}}, tmp_path)
    with pytest.raises(WorkflowError):
        cfg.workspace_root()


# --- finding 5: validate_dispatch exercises agent/hooks getters -----------------

def test_validate_dispatch_rejects_invalid_agent_values(tmp_path, monkeypatch):
    from orchestrator.workflow import validate_dispatch
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    cfg = _cfg({"tracker": {"kind": "github", "repo": "a/b"},
                "agent": {"max_turns": -1}}, tmp_path)
    with pytest.raises(WorkflowError):
        validate_dispatch(cfg)


# --- finding 6: ASCII-only workspace keys ---------------------------------------

def test_sanitize_rejects_unicode_alnum():
    assert sanitize_workspace_key("café/№42") == "caf___42"
    assert sanitize_workspace_key("ABC-123.x_y") == "ABC-123.x_y"


# --- finding 2: guard wired via --settings ---------------------------------------

def test_runner_injects_guard_settings(tmp_path):
    ws = tmp_path / "7"
    ws.mkdir()
    settings_path = _write_guard_settings(ws)
    assert settings_path.parent == tmp_path          # sibling, never inside
    assert not str(settings_path).startswith(str(ws))
    data = json.loads(settings_path.read_text())
    hook = data["hooks"]["PreToolUse"][0]
    assert "Write" in hook["matcher"]
    assert str(GUARD_PATH) in hook["hooks"][0]["command"]

    cfg = ClaudeConfig(command="claude -p", max_turns=1, max_budget_usd=None,
                       turn_timeout_ms=1000, read_timeout_ms=1000,
                       stall_timeout_ms=0)
    cmd = ClaudeRunner(cfg)._build_command(None, settings_path)
    assert f"--settings {str(settings_path)}" in cmd


# --- guard behavior (deny outside workspace, allow inside) -----------------------

def _run_guard(payload: dict, workspace: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(GUARD_PATH)],
        input=json.dumps(payload), capture_output=True, text=True,
        env={"CLAUDE_PROJECT_DIR": str(workspace), "PATH": "/usr/bin:/bin"},
    )


def test_guard_denies_write_outside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _run_guard({"tool_name": "Write",
                    "tool_input": {"file_path": str(tmp_path / "escape.txt")}}, ws)
    assert r.returncode == 2
    assert "denied" in r.stderr


def test_guard_allows_write_inside_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _run_guard({"tool_name": "Write",
                    "tool_input": {"file_path": str(ws / "ok.txt")}}, ws)
    assert r.returncode == 0


def test_guard_allows_relative_path_inside(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _run_guard({"tool_name": "Edit",
                    "tool_input": {"file_path": "src/x.py"}}, ws)
    assert r.returncode == 0


def test_guard_denies_dotdot_escape(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    r = _run_guard({"tool_name": "Edit",
                    "tool_input": {"file_path": "../outside.py"}}, ws)
    assert r.returncode == 2


# --- finding 4: broken workflow reload blocks dispatch ----------------------------

WORKFLOW = """---
tracker:
  kind: github
  repo: "acme/api"
  api_key: "tok"
polling:
  interval_ms: 100
workspace:
  root: "{root}"
claude:
  command: "unused"
---
Body {{{{ issue.identifier }}}}
"""


async def test_broken_reload_blocks_dispatch(tmp_path):
    from orchestrator.scheduler import Orchestrator

    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW.format(root=tmp_path / "ws"))
    orch = Orchestrator(wf)
    orch._load_workflow(initial=True)

    calls = {"fetch": 0}

    class T:
        async def fetch_candidate_issues(self):
            calls["fetch"] += 1
            return []

        async def fetch_issue_states_by_ids(self, ids):
            return []

        async def fetch_issues_by_states(self, s):
            return []

        async def add_issue_comment(self, i, b):
            pass

    real = orch._components
    orch._components = lambda: (T(), real()[1], real()[2])

    await orch._tick()
    assert calls["fetch"] == 1  # healthy: dispatch path reached the tracker

    wf.write_text("---\n: bad yaml [\n---\nbody")  # corrupt the workflow file
    orch._workflow_mtime = None                    # force change detection
    await orch._tick()
    assert calls["fetch"] == 1  # §5.5: no new candidate fetch while broken

    wf.write_text(WORKFLOW.format(root=tmp_path / "ws"))  # fix the file
    orch._workflow_mtime = None
    await orch._tick()
    assert calls["fetch"] == 2  # dispatch resumes
