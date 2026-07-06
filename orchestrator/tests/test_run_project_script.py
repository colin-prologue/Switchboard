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
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "project.env").write_text(
        f'SB_GITHUB_REPO=acme/widgets\nSB_BASE_BRANCH=main\n'
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
