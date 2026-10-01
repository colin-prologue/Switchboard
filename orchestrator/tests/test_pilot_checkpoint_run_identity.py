"""Behavioral safety tests for the explicit-Codex pilot run identity."""

import os
import stat
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = REPO_ROOT / "scripts" / "run-self-pilot-checkpoint.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def pilot_env(tmp_path: Path) -> dict[str, str]:
    """A no-network command harness which records every external invocation."""
    sb_home = tmp_path / "switchboard"
    (sb_home / ".git").mkdir(parents=True)
    checkpoints = sb_home / "projects" / "switchboard-self" / "pilot-checkpoints"
    checkpoints.mkdir(parents=True)
    (checkpoints / "01-explicit-codex.md").write_text("body", encoding="utf-8")
    (sb_home / "projects" / "switchboard-self" / "WORKFLOW.pilot-codex.md").write_text("workflow", encoding="utf-8")

    home = tmp_path / "home"
    app_env = home / ".config" / "switchboard" / "app.env"
    app_env.parent.mkdir(parents=True)
    app_env.write_text("SB_APP_ID=id\nSB_APP_INSTALLATION_ID=installation\nSB_APP_PRIVATE_KEY_FILE=key\nSB_APP_BOT_LOGIN=bot\nSB_APP_BOT_USER_ID=user\n", encoding="utf-8")
    (sb_home / "projects" / "switchboard-self" / "project.env").write_text("SB_GITHUB_REPO=colin-prologue/Switchboard\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "calls.log"
    _executable(fake_bin / "git", '''printf 'git %s\\n' "$*" >> "$FAKE_CALLS"
case " $* " in
  *" branch --show-current "*) printf 'main\\n' ;;
  *" status --porcelain "*) : ;;
  *" pull "*) : ;;
esac''')
    _executable(fake_bin / "gh", '''printf 'gh %s\\n' "$*" >> "$FAKE_CALLS"
case " $* " in
  *" auth status "*) : ;;
  *" label list "*) printf '%s\\n' status:todo status:in-progress status:human-review status:parked status:fail-review gate:triage-passed gate:fail-reviewed agent:codex provider:claude provider:codex ;;
  *" issue view "*) printf 'CLOSED\\n' ;;
  *" api "*)
    [ "${GH_API_EXIT:-0}" = 0 ] || exit "$GH_API_EXIT"
    printf '%s\\n' "${GH_API_OUTPUT:-${GH_TITLE_COUNT:-0}}"
    ;;
  *" issue create "*) printf 'UNEXPECTED_WRITE\\n' >&2; exit 97 ;;
esac''')
    for name in ("uv", "python3", "codex", "claude"):
        _executable(fake_bin / name, 'printf "' + name + ' %s\\n" "$*" >> "$FAKE_CALLS"')
    _executable(fake_bin / "pgrep", 'printf "pgrep %s\\n" "$*" >> "$FAKE_CALLS"\nexit 1')

    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "SB_HOME": str(sb_home),
        "FAKE_CALLS": str(calls),
        "PATH": str(fake_bin) + os.pathsep + env["PATH"],
    })
    return env


def run_launcher(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(LAUNCHER), *args], env=env, text=True, capture_output=True, check=False)


def calls(env: dict[str, str]) -> list[str]:
    path = Path(env["FAKE_CALLS"])
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


@pytest.mark.parametrize("args", [
    ("explicit-codex",),
    ("explicit-codex", "--run-id", "Bad"),
    ("explicit-codex", "--run-id", "bad/id"),
    ("explicit-codex", "--run-id", "x" * 41),
    ("explicit-codex", "--run-id", "one", "--run-id", "two"),
    ("explicit-codex", "--run-id", "one", "--resume"),
    ("rollback", "--preflight"),
])
def test_invalid_or_unsupported_identity_inputs_fail_before_external_actions(pilot_env, args):
    result = run_launcher(pilot_env, *args)
    assert result.returncode != 0
    assert calls(pilot_env) == []


def test_preflight_checks_title_before_pull_and_has_no_side_effects(pilot_env):
    result = run_launcher(pilot_env, "explicit-codex", "--run-id", "october-01", "--preflight")
    assert result.returncode == 0, result.stderr
    assert "PREFLIGHT TITLE AVAILABLE" in result.stdout
    recorded = calls(pilot_env)
    assert any(line.startswith("gh api ") and "--paginate" in line and "state=all" in line for line in recorded)
    assert not any(" pull " in line or "issue create" in line or line.startswith("uv ") for line in recorded)


def test_preflight_blocks_an_exact_title_found_beyond_the_first_page(pilot_env):
    pilot_env["GH_TITLE_COUNT"] = "1"  # fake paginated API found an older matching issue
    result = run_launcher(pilot_env, "explicit-codex", "--run-id", "october-01", "--preflight")
    assert result.returncode != 0
    assert "checkpoint already exists" in result.stderr
    assert not any(" pull " in line or "issue create" in line for line in calls(pilot_env))


@pytest.mark.parametrize("env_name, value", [("GH_API_EXIT", "86"), ("GH_API_OUTPUT", "not-a-count")])
def test_preflight_gh_api_failures_fail_closed_without_side_effects(pilot_env, env_name, value):
    pilot_env[env_name] = value
    result = run_launcher(pilot_env, "explicit-codex", "--run-id", "october-01", "--preflight")
    assert result.returncode != 0
    recorded = calls(pilot_env)
    assert any(line.startswith("gh api ") for line in recorded)
    assert not any(" pull " in line or "issue create" in line or line.startswith("uv ") for line in recorded)


def test_live_gh_api_failure_stops_after_original_update_before_any_write(pilot_env):
    pilot_env["GH_API_EXIT"] = "86"
    result = run_launcher(pilot_env, "explicit-codex", "--run-id", "october-01")
    assert result.returncode != 0
    recorded = calls(pilot_env)
    pull_index = next(i for i, line in enumerate(recorded) if " pull --ff-only origin main" in line)
    api_index = next(i for i, line in enumerate(recorded) if line.startswith("gh api "))
    assert pull_index < api_index
    assert not any("issue create" in line or line.startswith("uv ") for line in recorded)


def test_live_collision_preserves_update_then_fail_closed_order(pilot_env):
    pilot_env["GH_TITLE_COUNT"] = "1"
    result = run_launcher(pilot_env, "explicit-codex", "--run-id", "october-01")
    assert result.returncode != 0
    assert "checkpoint already exists" in result.stderr
    recorded = calls(pilot_env)
    pull_index = next(i for i, line in enumerate(recorded) if " pull --ff-only origin main" in line)
    collision_index = next(i for i, line in enumerate(recorded) if line.startswith("gh api "))
    assert pull_index < collision_index
    assert not any("issue create" in line or line.startswith("uv ") for line in recorded)


def test_run_id_is_in_title_and_durable_evidence_record():
    text = LAUNCHER.read_text(encoding="utf-8")
    assert 'TITLE="Self-pilot checkpoint 1 [$RUN_ID]:' in text
    assert 'RESULT-01-explicit-codex-$RUN_ID.md' in text
    assert "- run-id: %s" in text
    assert "resum" not in text.lower()
