"""Stance resolution in `scripts/register-project.sh` (issue #153).

Since `AgDR-043` the stance decides whether an AGENT may merge that project's own
PRs, so the flag that picks it is a permission decision rather than a workflow
preference. `--self` names Switchboard itself — the repo governing every other
project's merge rights — and inherited the general `prototype` default, which
would have granted its own workers the right to merge changes to `guard.py`.

The script talks to `gh` to provision labels, so every test puts a stub `gh`
first on PATH. The stub records nothing and succeeds: these tests are about the
composed binding, not the label calls.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "register-project.sh"


@pytest.fixture
def sb_home(tmp_path: Path) -> Path:
    """A skeleton carrying only what registration reads: the script and the
    templates it can resolve a stance to."""
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, home / "scripts" / "register-project.sh")
    (home / "workflow" / "stances").mkdir(parents=True)
    for src in (REPO_ROOT / "workflow" / "WORKFLOW.base.md",
                REPO_ROOT / "workflow" / "stances" / "WORKFLOW.prototype.md"):
        dst = (home / "workflow" / "stances" / src.name
               if "stances" in str(src) else home / "workflow" / src.name)
        shutil.copy(src, dst)

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    gh = stub_bin / "gh"
    gh.write_text("#!/bin/bash\nexit 0\n")
    gh.chmod(0o755)
    return home


def register(sb_home: Path, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}"
    env["SB_WORKSPACE_BASE"] = str(tmp_path / "ws")
    env["HOME"] = str(tmp_path / "home")
    return subprocess.run(
        ["bash", str(sb_home / "scripts" / "register-project.sh"), *args],
        env=env, capture_output=True, text=True)


def stance_of(sb_home: Path, slug: str) -> str:
    env_file = sb_home / "projects" / slug / "project.env"
    for line in env_file.read_text().splitlines():
        if line.startswith("SB_WORKFLOW_STANCE="):
            return line.split("=", 1)[1].strip()
    raise AssertionError(f"no SB_WORKFLOW_STANCE in {env_file}")


def test_self_defaults_to_base_not_prototype(sb_home, tmp_path):
    """The bug: a fresh Stage 4 would hand Switchboard's own workers the right
    to merge changes to the file that decides who may merge."""
    proc = register(sb_home, tmp_path, "--self", "--repo", "colin-prologue/Switchboard")
    assert proc.returncode == 0, proc.stderr
    assert stance_of(sb_home, "switchboard-self") == "base"


def test_self_binding_resolves_to_a_human_owned_gate(sb_home, tmp_path):
    """Asserted through the PROPERTY that matters rather than the stance NAME,
    so a future rename cannot slip past this."""
    from orchestrator.workflow import Config, load_workflow

    register(sb_home, tmp_path, "--self", "--repo", "colin-prologue/Switchboard")
    workflow = sb_home / "projects" / "switchboard-self" / "WORKFLOW.md"
    tracker = Config(load_workflow(workflow), workflow.parent).tracker()
    assert not tracker.agent_owns_gate_c()


@pytest.mark.parametrize("args", [
    ("--self", "--repo", "o/r", "--stance", "prototype"),
    ("--stance", "prototype", "--self", "--repo", "o/r"),   # order must not matter
])
def test_explicit_stance_still_wins_for_self(sb_home, tmp_path, args):
    """A safer default, not a prohibition. An operator who deliberately wants a
    loose self-stance may still have one — and `--self --stance X` must agree
    with `--stance X --self`, which is why resolution happens after the parse
    loop rather than inside the `--self` case."""
    proc = register(sb_home, tmp_path, *args)
    assert proc.returncode == 0, proc.stderr
    assert stance_of(sb_home, "switchboard-self") == "prototype"


def test_ordinary_registration_still_defaults_to_prototype(sb_home, tmp_path):
    """AgDR-039 chose `prototype` deliberately so a new project starts fast.
    This ticket narrows one case; it does not revisit that."""
    proc = register(sb_home, tmp_path, "--slug", "acme", "--repo", "acme/api")
    assert proc.returncode == 0, proc.stderr
    assert stance_of(sb_home, "acme") == "prototype"


def test_ordinary_registration_resolves_to_an_agent_owned_gate(sb_home, tmp_path):
    """The counterpart to the `--self` assertion: the general default must still
    grant what the stance ladder exists to grant."""
    from orchestrator.workflow import Config, load_workflow

    register(sb_home, tmp_path, "--slug", "acme", "--repo", "acme/api")
    workflow = sb_home / "projects" / "acme" / "WORKFLOW.md"
    tracker = Config(load_workflow(workflow), workflow.parent).tracker()
    assert tracker.agent_owns_gate_c()
