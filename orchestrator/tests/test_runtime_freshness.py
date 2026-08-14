"""Tests for scripts/freshness-preflight.sh (issue #32).

The preflight keeps a long-running orchestrator process current enough to
operate safely WITHOUT mutating or restarting its own checkout:

  * config (the workflow) is recomposed from `origin` into a gitignored,
    per-project `.run/<slug>/composed-WORKFLOW.md` and hot-reloaded by mtime;
  * loaded CODE staleness cannot be hot-reloaded, so it becomes a loud,
    queryable `restart-needed.json` marker instead.

The fixture builds a committed skeleton plus a LOCAL bare origin, both starting
at the same `orchestrator/src/` state (count 0, no marker) — the behind-origin
and ahead-only states are layered on top as explicit variants.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "scripts" / "freshness-preflight.sh"

BASE_TEMPLATE = """\
---
repo: {{REPO}}
workspace:
  root: {{WORKSPACE_ROOT}}
pool:
  max_concurrent_agents: {{MAX_AGENTS}}
convention_root: {{CONVENTION_ROOT}}
---
prompt body v1
"""

PROJECT_ENV = """\
SB_PROJECT_SLUG=demo
SB_GITHUB_REPO=acme/widgets
SB_BASE_BRANCH=main
SB_MAX_AGENTS=3
SB_WORKSPACE_ROOT=/tmp/ws
SB_CONVENTION_ROOT=
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def run_preflight(home: Path, slug: str = "demo", *, cwd: Path | None = None,
                  **env_extra: str) -> subprocess.CompletedProcess:
    """Invoke the preflight. SB_* is scrubbed so only what a test binds is seen."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SB_")}
    env["SB_HOME"] = str(home)
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(home / "scripts" / "freshness-preflight.sh"), slug],
        env=env, cwd=str(cwd) if cwd else str(home),
        capture_output=True, text=True)


def add_project(home: Path, slug: str, *, max_agents: str = "3",
                repo: str = "acme/widgets") -> Path:
    proj = home / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "project.env").write_text(
        f"SB_PROJECT_SLUG={slug}\nSB_GITHUB_REPO={repo}\nSB_BASE_BRANCH=main\n"
        f"SB_MAX_AGENTS={max_agents}\nSB_WORKSPACE_ROOT=/tmp/ws\n"
        f"SB_CONVENTION_ROOT=\n")
    (proj / "WORKFLOW.md").write_text("tracked snapshot\n")
    return proj


@pytest.fixture
def repo(tmp_path: Path):
    """Committed skeleton + local bare origin at the SAME orchestrator/src state."""
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, home / "scripts" / "freshness-preflight.sh")
    (home / "workflow").mkdir()
    (home / "workflow" / "WORKFLOW.base.md").write_text(BASE_TEMPLATE)
    (home / "orchestrator" / "src").mkdir(parents=True)
    (home / "orchestrator" / "src" / "mod.py").write_text("v1\n")
    (home / ".gitignore").write_text(".run/\n")
    add_project(home, "demo")

    git(home, "init", "-b", "main", "-q")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "skeleton")

    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", "-q", str(bare))
    git(home, "remote", "add", "origin", str(bare))
    git(home, "push", "-q", "origin", "main")

    # A second working clone is how tests advance origin without touching `home`.
    up = tmp_path / "up"
    git(tmp_path, "clone", "-q", str(bare), str(up))
    return home, bare, up


def advance_origin(up: Path, *, src: str | None = None, workflow: str | None = None,
                   msg: str = "advance") -> None:
    if src is not None:
        (up / "orchestrator" / "src" / "mod.py").write_text(src)
    if workflow is not None:
        (up / "workflow" / "WORKFLOW.base.md").write_text(workflow)
    git(up, "add", "-A")
    git(up, "commit", "-qm", msg)
    git(up, "push", "-q", "origin", "main")


def composed(home: Path, slug: str = "demo") -> Path:
    return home / ".run" / slug / "composed-WORKFLOW.md"


def marker(home: Path, slug: str = "demo") -> Path:
    return home / ".run" / slug / "restart-needed.json"


# --- recompose ---------------------------------------------------------------

def test_recomposes_from_origin_with_all_four_placeholders(repo):
    home, _, _ = repo
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    text = composed(home).read_text()
    assert "{{" not in text, f"unsubstituted placeholder survived:\n{text}"
    assert "repo: acme/widgets" in text
    assert "max_concurrent_agents: 3" in text
    assert "convention_root: \n" in text  # empty CONVENTION_ROOT is legal


def test_absent_template_key_defaults_to_base(repo):
    """switchboard-self carries no SB_WORKFLOW_TEMPLATE — it must default to
    `base`, agreeing with verify-setup.sh:107 and the CI drift check."""
    home, _, _ = repo
    env_file = home / "projects" / "demo" / "project.env"
    assert "SB_WORKFLOW_TEMPLATE" not in env_file.read_text()
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert "prompt body v1" in composed(home).read_text()


def test_origin_change_is_adopted_and_two_changes_both_land(repo):
    home, _, up = repo
    sha = git(home, "rev-parse", "HEAD")
    run_preflight(home, SB_LAUNCH_SHA=sha)

    advance_origin(up, workflow=BASE_TEMPLATE.replace("v1", "v2"), msg="wf v2")
    run_preflight(home, SB_LAUNCH_SHA=sha)
    assert "prompt body v2" in composed(home).read_text()

    advance_origin(up, workflow=BASE_TEMPLATE.replace("v1", "v3"), msg="wf v3")
    run_preflight(home, SB_LAUNCH_SHA=sha)
    assert "prompt body v3" in composed(home).read_text()


def test_unchanged_origin_does_not_touch_mtime(repo):
    """Renaming unconditionally would defeat scheduler.py:368's mtime guard and
    log a spurious `workflow reloaded` on every single tick."""
    home, _, _ = repo
    sha = git(home, "rev-parse", "HEAD")
    run_preflight(home, SB_LAUNCH_SHA=sha)
    first = composed(home).stat().st_mtime_ns
    for _ in range(3):
        run_preflight(home, SB_LAUNCH_SHA=sha)
    assert composed(home).stat().st_mtime_ns == first


def test_hand_edited_composed_file_is_overwritten(repo):
    """No hand-edit skip rule: the output is gitignored generated scratch, and a
    silent refusal to adopt origin is this ticket's own bug class."""
    home, _, _ = repo
    sha = git(home, "rev-parse", "HEAD")
    run_preflight(home, SB_LAUNCH_SHA=sha)
    composed(home).write_text("hand edited\n")
    run_preflight(home, SB_LAUNCH_SHA=sha)
    assert "prompt body v1" in composed(home).read_text()


def test_output_is_per_project(repo):
    """One checkout runs three processes; an unscoped path would let the last
    writer's repo binding hijack every process."""
    home, _, _ = repo
    add_project(home, "other", max_agents="7", repo="acme/other")
    sha = git(home, "rev-parse", "HEAD")
    run_preflight(home, "demo", SB_LAUNCH_SHA=sha)
    run_preflight(home, "other", SB_LAUNCH_SHA=sha)
    assert "max_concurrent_agents: 3" in composed(home, "demo").read_text()
    assert "max_concurrent_agents: 7" in composed(home, "other").read_text()
    assert "acme/other" not in composed(home, "demo").read_text()


# --- the checkout is never mutated -------------------------------------------

def test_never_writes_tracked_files_and_never_moves_head(repo):
    home, _, up = repo
    before_head = git(home, "rev-parse", "HEAD")
    before_tracked = (home / "projects" / "demo" / "WORKFLOW.md").read_text()
    advance_origin(up, src="v2\n", workflow=BASE_TEMPLATE.replace("v1", "v9"))

    proc = run_preflight(home, SB_LAUNCH_SHA=before_head)
    assert proc.returncode == 0, proc.stderr

    assert git(home, "status", "--porcelain") == ""
    assert git(home, "rev-parse", "HEAD") == before_head
    assert (home / "projects" / "demo" / "WORKFLOW.md").read_text() == before_tracked


# --- restart-needed signal ---------------------------------------------------

def test_src_advance_raises_marker_measured_from_loaded_sha(repo):
    home, _, up = repo
    launched_at = git(home, "rev-parse", "HEAD")
    advance_origin(up, src="v2\n", msg="src v2")

    proc = run_preflight(home, SB_LAUNCH_SHA=launched_at)
    assert proc.returncode == 0, proc.stderr
    assert marker(home).exists()
    data = json.loads(marker(home).read_text())
    assert data["behind_commits"] > 0
    assert data["loaded_sha"] == launched_at
    assert data["loaded_sha_source"] == "launch-sha"
    assert data["target_sha"] != launched_at
    assert "RESTART NEEDED" in proc.stderr


def test_marker_survives_a_pull_that_moves_head_past_the_loaded_sha(repo):
    """The heart of the ticket: a HEAD-based detector would self-clear on the
    operator's own `git pull` while the process still runs pre-pull code."""
    home, _, up = repo
    launched_at = git(home, "rev-parse", "HEAD")
    advance_origin(up, src="v2\n", msg="src v2")
    git(home, "fetch", "-q", "origin", "main")
    git(home, "merge", "-q", "--ff-only", "origin/main")
    assert git(home, "rev-parse", "HEAD") != launched_at

    run_preflight(home, SB_LAUNCH_SHA=launched_at)
    data = json.loads(marker(home).read_text())
    assert data["behind_commits"] > 0
    assert data["loaded_sha"] != git(home, "rev-parse", "HEAD")


def test_marker_deleted_once_the_process_restarts_at_the_new_sha(repo):
    home, _, up = repo
    launched_at = git(home, "rev-parse", "HEAD")
    advance_origin(up, src="v2\n", msg="src v2")
    run_preflight(home, SB_LAUNCH_SHA=launched_at)
    assert marker(home).exists()

    # "Restart": the process relaunches, loading origin's current sha.
    git(home, "fetch", "-q", "origin", "main")
    restarted_at = git(home, "rev-parse", "origin/main")
    run_preflight(home, SB_LAUNCH_SHA=restarted_at)
    assert not marker(home).exists()


def test_marker_is_per_project(repo):
    home, _, up = repo
    add_project(home, "other")
    launched_at = git(home, "rev-parse", "HEAD")
    advance_origin(up, src="v2\n", msg="src v2")
    run_preflight(home, "demo", SB_LAUNCH_SHA=launched_at)
    run_preflight(home, "other", SB_LAUNCH_SHA=launched_at)
    assert marker(home, "demo").exists() and marker(home, "other").exists()

    # 'other' restarts; 'demo' is still running old code and keeps its marker.
    git(home, "fetch", "-q", "origin", "main")
    run_preflight(home, "other", SB_LAUNCH_SHA=git(home, "rev-parse", "origin/main"))
    assert not marker(home, "other").exists()
    assert marker(home, "demo").exists()


def test_ahead_only_checkout_raises_no_marker(repo):
    """A local commit origin has not seen is not staleness."""
    home, _, _ = repo
    (home / "orchestrator" / "src" / "mod.py").write_text("local\n")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "local only")
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert not marker(home).exists()


def test_workflow_only_change_updates_config_and_raises_no_marker(repo):
    home, _, up = repo
    sha = git(home, "rev-parse", "HEAD")
    advance_origin(up, workflow=BASE_TEMPLATE.replace("v1", "v42"), msg="wf only")
    proc = run_preflight(home, SB_LAUNCH_SHA=sha)
    assert proc.returncode == 0, proc.stderr
    assert "prompt body v42" in composed(home).read_text()
    assert not marker(home).exists()


def test_unbound_launch_sha_degrades_loudly_to_head(repo):
    home, _, up = repo
    advance_origin(up, src="v2\n", msg="src v2")
    proc = run_preflight(home)  # SB_LAUNCH_SHA deliberately unset
    assert proc.returncode == 0, proc.stderr
    assert "SB_LAUNCH_SHA unbound" in proc.stderr
    assert json.loads(marker(home).read_text())["loaded_sha_source"] == "HEAD-fallback"


# --- fail-open ---------------------------------------------------------------

def test_missing_repo_fails_open_and_seeds_from_tracked_workflow(tmp_path):
    """The repo-less skeleton IS the missing-repo fail-open state."""
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, home / "scripts" / "freshness-preflight.sh")
    add_project(home, "demo")
    proc = run_preflight(home)
    assert proc.returncode == 0, proc.stderr
    assert "no git repository" in proc.stderr
    assert composed(home).read_text() == "tracked snapshot\n"


def test_missing_repo_reported_before_binding_gaps(tmp_path):
    """Check order is pinned: reachability BEFORE binding completeness, so a
    repo-less deploy names the real cause rather than an incidental one."""
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, home / "scripts" / "freshness-preflight.sh")
    proj = home / "projects" / "demo"
    proj.mkdir(parents=True)
    (proj / "project.env").write_text("SB_GITHUB_REPO=acme/widgets\n")  # no MAX_AGENTS
    (proj / "WORKFLOW.md").write_text("tracked snapshot\n")
    proc = run_preflight(home)
    assert proc.returncode == 0
    assert "no git repository" in proc.stderr
    assert "SB_MAX_AGENTS" not in proc.stderr


def test_absent_max_agents_skips_whole_recompose(repo):
    """No safe default exists, and emitting a literal {{MAX_AGENTS}} would hand
    the loader a corrupt workflow."""
    home, _, _ = repo
    env_file = home / "projects" / "demo" / "project.env"
    env_file.write_text(
        env_file.read_text().replace("SB_MAX_AGENTS=3\n", ""))
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert "SB_MAX_AGENTS missing" in proc.stderr
    assert "{{MAX_AGENTS}}" not in composed(home).read_text()
    assert composed(home).read_text() == "tracked snapshot\n"


def test_unreachable_remote_fails_open(repo):
    home, bare, _ = repo
    shutil.rmtree(bare)
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert "fetch of origin/main failed" in proc.stderr
    assert composed(home).read_text() == "tracked snapshot\n"


def test_missing_template_file_at_origin_fails_open(repo):
    """Distinct from an absent KEY, which defaults to `base` and recomposes."""
    home, _, up = repo
    (up / "workflow" / "WORKFLOW.base.md").unlink()
    git(up, "add", "-A")
    git(up, "commit", "-qm", "drop template")
    git(up, "push", "-q", "origin", "main")
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert "absent at origin/main" in proc.stderr


def test_unknown_template_fails_open(repo):
    home, _, _ = repo
    env_file = home / "projects" / "demo" / "project.env"
    env_file.write_text(env_file.read_text() + "SB_WORKFLOW_TEMPLATE=nope\n")
    proc = run_preflight(home, SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert "unknown SB_WORKFLOW_TEMPLATE 'nope'" in proc.stderr


def test_transient_fetch_failure_after_success_preserves_content_and_mtime(repo):
    """A seed here would bump the mtime, parse cleanly, and silently revert the
    running process to the committed snapshot — the inverse of this ticket."""
    home, bare, _ = repo
    sha = git(home, "rev-parse", "HEAD")
    run_preflight(home, SB_LAUNCH_SHA=sha)
    good = composed(home).read_text()
    mtime = composed(home).stat().st_mtime_ns

    shutil.rmtree(bare)
    proc = run_preflight(home, SB_LAUNCH_SHA=sha)
    assert proc.returncode == 0
    assert composed(home).read_text() == good
    assert composed(home).stat().st_mtime_ns == mtime


def test_usage_error_is_the_only_nonzero_exit(repo):
    home, _, _ = repo
    proc = subprocess.run(
        ["bash", str(home / "scripts" / "freshness-preflight.sh")],
        env={**os.environ, "SB_HOME": str(home)}, capture_output=True, text=True)
    assert proc.returncode != 0
    assert "usage:" in proc.stderr


# --- execution context -------------------------------------------------------

def test_all_paths_anchor_to_sb_home_not_cwd(repo, tmp_path):
    """At launch this runs BEFORE run-project.sh pins the cwd, so CWD is
    arbitrary. Without anchoring, a repo-less skeleton test would fetch the real
    origin and write .run/ artifacts into the live checkout."""
    home, _, _ = repo
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = run_preflight(home, cwd=elsewhere,
                         SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert composed(home).exists()
    assert not (elsewhere / ".run").exists()


def test_sb_home_derived_from_own_location_when_unbound(repo):
    home, _, _ = repo
    env = {k: v for k, v in os.environ.items() if not k.startswith("SB_")}
    proc = subprocess.run(
        ["bash", str(home / "scripts" / "freshness-preflight.sh"), "demo"],
        env=env, cwd=str(home.parent), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert composed(home).exists()


def test_self_base_branch_override_is_honoured(repo):
    home, _, up = repo
    git(up, "checkout", "-qb", "release")
    advance_origin(up, workflow=BASE_TEMPLATE.replace("v1", "from-release"),
                   msg="release wf")
    git(up, "push", "-q", "origin", "release")
    proc = run_preflight(home, SB_SELF_BASE_BRANCH="release",
                         SB_LAUNCH_SHA=git(home, "rev-parse", "HEAD"))
    assert proc.returncode == 0, proc.stderr
    assert "prompt body from-release" in composed(home).read_text()


# --- tracked-tree invariants -------------------------------------------------

def test_every_tracked_project_env_carries_max_agents():
    """The recompose reads SB_MAX_AGENTS from the LOCAL binding; a project
    missing it silently takes the skip path forever."""
    envs = sorted((REPO_ROOT / "projects").glob("*/project.env"))
    assert envs, "no tracked projects found"
    for env_file in envs:
        assert "SB_MAX_AGENTS=" in env_file.read_text(), f"{env_file} lacks SB_MAX_AGENTS"


def test_switchboard_self_binding_matches_its_tracked_workflow():
    """Seeds the accepted verify-setup.sh:114 blind spot at zero divergence.
    switchboard-self is the only project with no binding test, the only value
    != 1, and the only one exercising the template-key default."""
    env_file = REPO_ROOT / "projects" / "switchboard-self" / "project.env"
    wf = REPO_ROOT / "projects" / "switchboard-self" / "WORKFLOW.md"
    bound = next(line.split("=", 1)[1].strip()
                 for line in env_file.read_text().splitlines()
                 if line.startswith("SB_MAX_AGENTS="))
    derived = next(line.split(":", 1)[1].strip()
                   for line in wf.read_text().splitlines()
                   if line.strip().startswith("max_concurrent_agents:"))
    assert bound == derived, (
        f"SB_MAX_AGENTS={bound} diverges from WORKFLOW.md's {derived}")
