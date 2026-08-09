"""Worker merge guard — Gate C by mechanism (issue #133, AgDR-036).

The guard denies an ENUMERATED set of Gate-C-violating Bash shapes. It is not a
security boundary (denials are soft; `gh api …/pulls/{n}/merge` is a recorded
residual) — it raises the cost of a violation and makes every attempt
observable. These tests pin the enumeration in both directions: the denied
shapes deny, and the free-text/read-only shapes that share their tokens allow.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.runner import GUARD_MATCHER, GUARD_PATH, _write_guard_settings

DENIAL_PREFIX = "switchboard-guard: denied:"


def _run_guard_env(payload: dict, env: dict) -> subprocess.CompletedProcess:
    """`_run_guard` (test_audit_fixes.py:77) hard-codes CLAUDE_PROJECT_DIR and
    so cannot express the no-workspace case; this variant takes the env."""
    return subprocess.run(
        [sys.executable, "-I", str(GUARD_PATH)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def _run_bash(command: str, workspace: Path) -> subprocess.CompletedProcess:
    return _run_guard_env(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"CLAUDE_PROJECT_DIR": str(workspace), "PATH": "/usr/bin:/bin"},
    )


DENIED = [
    # gh pr merge — any flag order, with/without -R o/r
    "gh pr merge 12",
    "gh pr merge 12 --squash --delete-branch",
    "gh pr merge --squash -R colin-prologue/Switchboard 12",
    "gh pr merge",
    # gh pr review --approve — flag before or after the PR number
    "gh pr review 12 --approve",
    "gh pr review --approve 12",
    'gh pr review 12 --approve --body "lgtm"',
    # gh pr close
    "gh pr close 12",
    "gh pr close 12 --comment 'abandoned'",
    # force pushes
    "git push --force",
    "git push --force origin switchboard/issue-133",
    "git push --force-with-lease",
    "git push --force-with-lease origin switchboard/issue-133",
    "git push -f",
    "git push -f origin switchboard/issue-133",
    # the +refspec force form
    "git push origin +switchboard/issue-133",
    "git push origin +refs/heads/main:refs/heads/main",
]

ALLOWED = [
    # read-only / non-merging gh pr verbs
    "gh pr view 12",
    "gh pr comment 12 --body 'ready for review'",
    "gh pr diff 12",
    "gh pr create --title 'merge guard' --body 'implements #133'",
    "gh pr review 12 --comment --body 'a note'",
    # non-force pushes
    "git push",
    "git push origin switchboard/issue-133",
    "git push -u origin switchboard/issue-133",
    # gh api is a recorded residual, not a matched shape
    "gh api repos/o/r/pulls/12",
    # free text: the verbs appear inside quoted arguments, never in verb
    # position. Denying these would strand the MANDATORY handoff step
    # (WORKFLOW.base.md:339-341) — this ticket's own PR first.
    'gh pr create --title "merge guard" --body "denies gh pr merge; a human will merge"',
    'gh pr comment 12 --body "ready to merge"',
    'gh issue comment 133 --body "… close …"',
    # anchor-binding: both fail under any first-`pr`-anywhere reading
    'gh issue comment 133 --body "the guard denies gh pr merge here"',
    'gh pr comment 12 --body "gh pr merge is Colin\'s call"',
]


@pytest.mark.parametrize("command", DENIED)
def test_guard_denies_gate_c_shapes(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2, r
    assert r.stderr.startswith(DENIAL_PREFIX), r.stderr
    assert "hand off, don't self-merge" in r.stderr


@pytest.mark.parametrize("command", ALLOWED)
def test_guard_allows_everything_else(command, tmp_path):
    r = _run_bash(command, tmp_path)
    # A PreToolUse hook's "allow" is exit 0 with empty stdout — there is no
    # stdin passthrough.
    assert r.returncode == 0, r
    assert r.stdout == ""


def test_merge_deny_is_workspace_independent():
    """No CLAUDE_PROJECT_DIR and no `cwd` in the payload: the merge deny must
    not ride behind guard.py's no-workspace early return."""
    r = _run_guard_env(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 12"}},
        {"PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 2, r
    assert r.stderr.startswith(DENIAL_PREFIX), r.stderr


def test_unbalanced_quote_falls_back_to_naive_split(tmp_path):
    """shlex raises on an unbalanced quote; the fallback must still evaluate,
    never silently allow."""
    r = _run_bash("gh pr merge 12 --body 'oops", tmp_path)
    assert r.returncode == 2, r


def test_bashoutput_is_a_harmless_matcher_superset(tmp_path):
    """The settings matcher alternation is unanchored, so `Bash` also matches
    `BashOutput` — no `command` key => exit 0."""
    r = _run_guard_env(
        {"tool_name": "BashOutput", "tool_input": {"bash_id": "1"}},
        {"CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0
    assert r.stdout == ""


def test_guard_settings_matcher_includes_bash(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    matcher = json.loads(_write_guard_settings(ws).read_text())[
        "hooks"]["PreToolUse"][0]["matcher"]
    assert matcher == GUARD_MATCHER
    assert "Bash" in matcher.split("|")
    # additive: the containment matchers survive
    for old in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert old in matcher.split("|")
