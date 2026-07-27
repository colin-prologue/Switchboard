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


# --- issue #57: reuse-branch freshness + .sb-staleness surfacing -------------

def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True,
                   capture_output=True, text=True)


def git_out(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def origin_of(ws: Path) -> Path:
    return ws.parent / "origin"


def commit_in(repo: Path, name: str, content: str, msg: str) -> str:
    """Commit a file on repo's current branch; return the new HEAD sha."""
    (repo / name).write_text(content)
    git(repo, "add", ".")
    git(repo, "-c", "user.name=X", "-c", "user.email=x@example.com",
        "commit", "-q", "-m", msg)
    return git_out(repo, "rev-parse", "HEAD")


def read_staleness(ws: Path) -> dict[str, str] | None:
    f = ws / ".sb-staleness"
    if not f.exists():
        return None
    out: dict[str, str] = {}
    for line in f.read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def derive_behind(ws: Path) -> str:
    """Behind-count the same way the hook does — never hard-coded."""
    return git_out(ws, "rev-list", "--count", "HEAD..origin/main")


def reuse(workspace: Path) -> Path:
    """Drive the first (create) run so the per-issue branch exists, leaving the
    workspace checked out on it for a subsequent reuse-path run."""
    proc = run_hook(workspace, {})
    assert proc.returncode == 0, proc.stderr
    assert git_out(workspace, "rev-parse", "--abbrev-ref", "HEAD") \
        == "switchboard/issue-7"
    return workspace


def test_reuse_clean_fast_forwards_to_origin(workspace: Path):
    ws = reuse(workspace)
    new_head = commit_in(origin_of(ws), "adv.txt", "advanced\n", "advance origin")

    proc = run_hook(ws, {})
    assert proc.returncode == 0, proc.stderr

    assert git_out(ws, "rev-parse", "HEAD") == new_head
    assert git_out(ws, "rev-parse", "origin/main") == new_head
    assert read_staleness(ws) is None


def test_reuse_dirty_skips_merge_and_records_reason(workspace: Path):
    ws = reuse(workspace)
    before_head = git_out(ws, "rev-parse", "HEAD")
    # Dirty file deliberately does NOT overlap the origin advance — the case a
    # bare `git merge --ff-only` would silently apply.
    (ws / "local-scratch.txt").write_text("work in progress\n")
    dirty_bytes = (ws / "local-scratch.txt").read_bytes()
    commit_in(origin_of(ws), "adv.txt", "advanced\n", "advance origin")

    proc = run_hook(ws, {})
    assert proc.returncode == 0, proc.stderr

    assert git_out(ws, "rev-parse", "HEAD") == before_head          # unmoved
    assert (ws / "local-scratch.txt").read_bytes() == dirty_bytes   # byte-identical
    assert git_out(ws, "diff") == ""                                # no tracked change

    stale = read_staleness(ws)
    assert stale is not None
    assert stale["reason"] == "dirty"
    assert stale["behind-count"] == derive_behind(ws)
    assert stale["workspace-head"] == before_head


def test_reuse_ahead_only_no_merge_no_staleness(workspace: Path):
    ws = reuse(workspace)
    local_head = commit_in(ws, "feature.txt", "local work\n", "local commit")
    # origin NOT advanced.

    proc = run_hook(ws, {})
    assert proc.returncode == 0, proc.stderr

    assert git_out(ws, "rev-parse", "HEAD") == local_head  # commit survives
    assert read_staleness(ws) is None                      # origin is an ancestor


def test_reuse_diverged_skips_and_records_reason(workspace: Path):
    ws = reuse(workspace)
    local_head = commit_in(ws, "feature.txt", "local work\n", "local commit")
    commit_in(origin_of(ws), "adv.txt", "advanced\n", "advance origin")

    proc = run_hook(ws, {})
    assert proc.returncode == 0, proc.stderr

    assert git_out(ws, "rev-parse", "HEAD") == local_head  # unmoved, survives
    stale = read_staleness(ws)
    assert stale is not None
    assert stale["reason"] == "diverged"
    assert stale["behind-count"] == derive_behind(ws)


def test_fetch_failure_errors_attempt_and_leaves_tree(workspace: Path):
    ws = reuse(workspace)
    before_head = git_out(ws, "rev-parse", "HEAD")
    git(ws, "remote", "set-url", "origin", str(ws.parent / "does-not-exist"))

    proc = run_hook(ws, {})
    assert proc.returncode != 0                       # attempt errors (§9.3)
    assert git_out(ws, "rev-parse", "HEAD") == before_head
    assert read_staleness(ws) is None


def test_fresh_then_stale_round_trip_removes_file(workspace: Path):
    ws = reuse(workspace)
    commit_in(origin_of(ws), "adv.txt", "advanced\n", "advance origin")
    # Untracked dirty file (non-overlapping) forces the dirty skip.
    (ws / "local-scratch.txt").write_text("wip\n")

    proc = run_hook(ws, {})
    assert proc.returncode == 0, proc.stderr
    assert read_staleness(ws) is not None  # stale: file present

    # Resolve the dirtiness; now clean + behind-only ⇒ clean ff.
    (ws / "local-scratch.txt").unlink()
    origin_head = git_out(origin_of(ws), "rev-parse", "HEAD")

    proc = run_hook(ws, {})
    assert proc.returncode == 0, proc.stderr
    assert git_out(ws, "rev-parse", "HEAD") == origin_head
    assert read_staleness(ws) is None  # file removed
