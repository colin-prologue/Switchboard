"""Tests for scripts/run-project.sh credential sourcing (issue #10).

The script derives SB_HOME from its own location, so each test copies it into
a tmp skeleton (scripts/ + projects/demo/) and points HOME at a tmp home. The
orchestrator exec is replaced by a dump script that records the final env.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run-project.sh"
PREFLIGHT = REPO_ROOT / "scripts" / "freshness-preflight.sh"

APP_ENV = """\
SB_APP_ID=4225392
SB_APP_INSTALLATION_ID=144657149
SB_APP_PRIVATE_KEY_FILE=/tmp/app.pem
SB_APP_BOT_LOGIN=switchboard-agent[bot]
SB_APP_BOT_USER_ID=300281474
"""


@pytest.fixture
def sb_home(tmp_path: Path) -> Path:
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, home / "scripts" / "run-project.sh")
    # issue #32: run-project.sh now invokes the preflight unconditionally. Under
    # `set -e` an absent script is exit 127 and would kill every launch test —
    # a script cannot fail open about its own absence.
    shutil.copy(PREFLIGHT, home / "scripts" / "freshness-preflight.sh")
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    # SB_MAX_AGENTS / SB_PROJECT_SLUG mirror production bindings; without the
    # former every recompose here would silently take the absent-key skip path.
    (project / "project.env").write_text(
        f'SB_PROJECT_SLUG=demo\nSB_GITHUB_REPO=acme/widgets\nSB_BASE_BRANCH=main\n'
        f'SB_MAX_AGENTS=2\n'
        f'SB_WORKSPACE_ROOT={tmp_path / "ws"}\n')
    (project / "WORKFLOW.md").write_text("---\n---\nprompt")
    dump = tmp_path / "dump.sh"
    dump.write_text("#!/bin/bash\nprintenv > \"$DUMP_OUT\"\n")
    dump.chmod(0o755)
    return home


def run_script(sb_home: Path, tmp_path: Path,
               *, with_app_env: bool, github_token: str | None):
    fake_home = tmp_path / "home"
    if with_app_env:
        cfg = fake_home / ".config" / "switchboard"
        cfg.mkdir(parents=True)
        (cfg / "app.env").write_text(APP_ENV)
    else:
        fake_home.mkdir(exist_ok=True)

    env = {k: v for k, v in os.environ.items()
           if k not in ("GITHUB_TOKEN", "HOME") and not k.startswith("SB_")}
    env["HOME"] = str(fake_home)
    env["DUMP_OUT"] = str(tmp_path / "env.out")
    env["SB_ORCHESTRATOR_CMD"] = f"bash {tmp_path / 'dump.sh'}"
    if github_token is not None:
        env["GITHUB_TOKEN"] = github_token

    proc = subprocess.run(
        ["bash", str(sb_home / "scripts" / "run-project.sh"), "demo"],
        env=env, capture_output=True, text=True)
    out_file = tmp_path / "env.out"
    dumped = out_file.read_text() if out_file.exists() else ""
    return proc, dict(
        line.split("=", 1) for line in dumped.splitlines() if "=" in line)


def test_app_env_sourced_and_exported_without_github_token(sb_home, tmp_path):
    proc, dumped = run_script(sb_home, tmp_path, with_app_env=True, github_token=None)
    assert proc.returncode == 0, proc.stderr
    # App identifiers reach the orchestrator AND the hooks' environment.
    assert dumped["SB_APP_ID"] == "4225392"
    assert dumped["SB_APP_INSTALLATION_ID"] == "144657149"
    assert dumped["SB_APP_BOT_LOGIN"] == "switchboard-agent[bot]"
    assert dumped["SB_GITHUB_REPO"] == "acme/widgets"  # project.env still wins its keys


def test_no_credentials_at_all_fails_with_message(sb_home, tmp_path):
    proc, _ = run_script(sb_home, tmp_path, with_app_env=False, github_token=None)
    assert proc.returncode != 0
    assert "credential" in proc.stderr.lower() or "GITHUB_TOKEN" in proc.stderr


def test_personal_token_alone_still_works(sb_home, tmp_path):
    proc, dumped = run_script(sb_home, tmp_path, with_app_env=False,
                              github_token="ghp_dogfood")
    assert proc.returncode == 0, proc.stderr
    assert dumped["GITHUB_TOKEN"] == "ghp_dogfood"
    assert "SB_APP_ID" not in dumped


# --- issue #32: launch-time freshness preflight -------------------------------

def test_repoless_skeleton_fails_open_with_the_missing_repo_line(sb_home, tmp_path):
    """The base skeleton is repo-less, so it IS the missing-repo fail-open
    state. It must still reach exec, and no unowned `fatal:` line from git's own
    stderr may precede the preflight's named warning."""
    proc, dumped = run_script(sb_home, tmp_path, with_app_env=False,
                              github_token="ghp_dogfood")
    assert proc.returncode == 0, proc.stderr
    assert "no git repository" in proc.stderr
    assert "fatal:" not in proc.stderr
    # An unbound launch sha is a fail-open state, never a launch refusal.
    assert "SB_LAUNCH_SHA" not in dumped


def test_launch_execs_against_the_composed_workflow(sb_home, tmp_path):
    """The exec path is unconditionally the composed file; fail-open seeds it
    from the tracked WORKFLOW.md rather than switching the path back."""
    proc, _ = run_script(sb_home, tmp_path, with_app_env=False,
                         github_token="ghp_dogfood")
    assert proc.returncode == 0, proc.stderr
    composed = sb_home / ".run" / "demo" / "composed-WORKFLOW.md"
    assert composed.read_text() == "---\n---\nprompt"


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True, text=True)


@pytest.fixture
def sb_home_with_origin(sb_home: Path, tmp_path: Path) -> Path:
    """Layer a committed skeleton + local bare origin onto the base fixture, at
    the SAME orchestrator/src state — count 0, no marker, no warnings."""
    (sb_home / "workflow").mkdir()
    (sb_home / "workflow" / "WORKFLOW.base.md").write_text(
        "---\nrepo: {{REPO}}\nroot: {{WORKSPACE_ROOT}}\n"
        "agents: {{MAX_AGENTS}}\nconv: {{CONVENTION_ROOT}}\n---\nbody\n")
    (sb_home / "orchestrator" / "src").mkdir(parents=True)
    (sb_home / "orchestrator" / "src" / "mod.py").write_text("v1\n")
    (sb_home / ".gitignore").write_text(".run/\n")
    _git(sb_home, "init", "-b", "main", "-q")
    _git(sb_home, "add", "-A")
    _git(sb_home, "commit", "-qm", "skeleton")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", "-q", str(bare))
    _git(sb_home, "remote", "add", "origin", str(bare))
    _git(sb_home, "push", "-q", "origin", "main")
    return sb_home


def test_successful_launch_recompose_is_silent(sb_home_with_origin, tmp_path):
    proc, _ = run_script(sb_home_with_origin, tmp_path, with_app_env=False,
                         github_token="ghp_dogfood")
    assert proc.returncode == 0, proc.stderr
    assert proc.stderr.strip() == "", f"unexpected warnings:\n{proc.stderr}"
    composed = sb_home_with_origin / ".run" / "demo" / "composed-WORKFLOW.md"
    assert "agents: 2" in composed.read_text()  # from the binding, via origin
    assert not (sb_home_with_origin / ".run" / "demo" / "restart-needed.json").exists()
    # The routine never writes a tracked file and never moves HEAD.
    status = subprocess.run(["git", "status", "--porcelain"],
                            cwd=str(sb_home_with_origin),
                            capture_output=True, text=True)
    assert status.stdout == ""


def test_launch_prints_the_restart_needed_marker_when_present(
        sb_home_with_origin, tmp_path):
    # Advance origin's orchestrator/src past the launching checkout, so the
    # marker is genuinely raised. (Planting one by hand proves nothing: it is
    # derived state, and the preflight rightly deletes a stale one.)
    up = tmp_path / "up"
    _git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(up))
    (up / "orchestrator" / "src" / "mod.py").write_text("v2\n")
    _git(up, "add", "-A")
    _git(up, "commit", "-qm", "src v2")
    _git(up, "push", "-q", "origin", "main")

    proc, _ = run_script(sb_home_with_origin, tmp_path, with_app_env=False,
                         github_token="ghp_dogfood")
    assert proc.returncode == 0, proc.stderr
    assert "restart-needed marker present" in proc.stdout
    assert "behind_commits" in proc.stdout
    assert "RESTART NEEDED" in proc.stderr


def test_app_env_missing_bot_identity_fails(sb_home, tmp_path):
    """Codex PR #42 P2: the launch-time check requires the full five-key set,
    not just the minting keys."""
    fake_home = tmp_path / "home"
    cfg = fake_home / ".config" / "switchboard"
    cfg.mkdir(parents=True)
    (cfg / "app.env").write_text(
        "SB_APP_ID=4225392\nSB_APP_INSTALLATION_ID=144657149\n"
        "SB_APP_PRIVATE_KEY_FILE=/tmp/app.pem\n")  # bot identity keys missing

    env = {k: v for k, v in os.environ.items()
           if k not in ("GITHUB_TOKEN", "HOME") and not k.startswith("SB_")}
    env["HOME"] = str(fake_home)
    env["DUMP_OUT"] = str(tmp_path / "env.out")
    env["SB_ORCHESTRATOR_CMD"] = f"bash {tmp_path / 'dump.sh'}"
    dump = tmp_path / "dump.sh"
    dump.write_text("#!/bin/bash\nprintenv > \"$DUMP_OUT\"\n")

    proc = subprocess.run(
        ["bash", str(sb_home / "scripts" / "run-project.sh"), "demo"],
        env=env, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "credential" in proc.stderr.lower()
