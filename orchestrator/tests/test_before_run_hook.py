"""Tests for hooks/before_run.sh — bot commit identity + push credentials
(issue #10). Exercised as a subprocess against a local clone; no network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "hooks" / "before_run.sh"

BOT_ENV = {
    "SB_APP_BOT_LOGIN": "switchboard-agent[bot]",
    "SB_APP_BOT_USER_ID": "300281474",
}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A per-issue workspace: a clone of a local 'origin' with one commit on main."""
    origin = tmp_path / "origin"
    origin.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=origin, check=True)
    (origin / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Seed", "-c", "user.email=seed@example.com",
         "commit", "-q", "-m", "seed"],
        cwd=origin, check=True)
    ws = tmp_path / "7"  # basename == issue number
    subprocess.run(["git", "clone", "-q", str(origin), str(ws)], check=True)
    return ws


def run_hook(ws: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, SB_BASE_BRANCH="main", **extra_env)
    for k in BOT_ENV:
        env.pop(k, None)
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK)], cwd=ws, env=env,
        capture_output=True, text=True)


def git_config(ws: Path, key: str) -> str | None:
    proc = subprocess.run(["git", "config", "--local", key],
                          cwd=ws, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def test_bot_identity_and_push_credentials_configured(workspace: Path):
    proc = run_hook(workspace, BOT_ENV)
    assert proc.returncode == 0, proc.stderr

    assert git_config(workspace, "user.name") == "switchboard-agent[bot]"
    assert git_config(workspace, "user.email") == \
        "300281474+switchboard-agent[bot]@users.noreply.github.com"

    helper = git_config(workspace, "credential.helper")
    assert helper is not None
    # x-access-token is GitHub's username for installation-token HTTPS auth;
    # the password must be read from env AT PUSH TIME (the agent's per-turn
    # fresh token), so the helper must reference the var, not embed a value.
    assert "x-access-token" in helper
    assert "$GITHUB_TOKEN" in helper


def test_without_bot_env_git_identity_untouched(workspace: Path):
    proc = run_hook(workspace, {})
    assert proc.returncode == 0, proc.stderr
    assert git_config(workspace, "user.name") is None
    assert git_config(workspace, "credential.helper") is None
