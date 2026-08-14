"""Regression coverage for the macOS LaunchAgent template (issue #112).

`plutil -lint` is not on the worker allowlist, so this is the only
in-workspace verification the deliverable has. The template therefore keeps
XML-safe `__SLUG__` / `__USER__` placeholders instead of angle-bracket tokens:
it must stay well-formed XML as checked in for `plistlib.loads()` to reach it.
"""

from __future__ import annotations

import plistlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "deploy" / "com.switchboard.__SLUG__.plist.template"

# The poll interval a crash loop must not outpace (workflow/WORKFLOW.base.md:16,
# polling.interval_ms: 30000).
POLL_INTERVAL_SECONDS = 30


def _load() -> dict:
    return plistlib.loads(TEMPLATE.read_bytes())


def test_template_is_parseable_and_has_the_required_keys() -> None:
    plist = _load()

    # Label is load-bearing: `launchctl load` rejects a plist without it, and
    # before this template only the filename implied it.
    for key in (
        "Label",
        "ProgramArguments",
        "EnvironmentVariables",
        "StandardOutPath",
        "StandardErrorPath",
        "KeepAlive",
        "ThrottleInterval",
        "RunAtLoad",
    ):
        assert key in plist, f"missing required key: {key}"

    assert plist["Label"] == "com.switchboard.__SLUG__"


def test_program_arguments_run_the_project_script_by_absolute_path() -> None:
    argv = _load()["ProgramArguments"]

    # A LaunchAgent's cwd is `/`, so a relative argv[0] is ENOENT and KeepAlive
    # turns it into a permanent spawn-failure retry.
    assert Path(argv[0]).is_absolute(), argv[0]
    assert argv[0].endswith("/scripts/run-project.sh"), argv[0]
    assert argv[1:] == ["__SLUG__"], argv


def test_both_log_paths_are_absolute_and_identical() -> None:
    plist = _load()
    out, err = plist["StandardOutPath"], plist["StandardErrorPath"]

    # Absolute because launchd does not expand `~` in plist string values; one
    # shared file so a stderr traceback can be read against the stdout banner.
    assert Path(out).is_absolute(), out
    assert out == err, (out, err)

    # Outside the repository and outside SB_WORKSPACE_ROOT — not merely
    # gitignored (the logs carry issue identifiers and error text).
    assert "/Developer/Switchboard" not in out, out
    assert "/switchboard-workspaces/" not in out, out


def test_restart_policy_is_rate_limited_and_leaves_a_clean_stop_stopped() -> None:
    plist = _load()

    # SuccessfulExit=false, not KeepAlive=true: the SIGTERM path exits 0
    # (main.py:77), so a deliberate `launchctl stop` must not restart.
    assert plist["KeepAlive"] == {"SuccessfulExit": False}

    # The ticket's central quantitative claim, checked numerically rather than
    # eyeballed in a comment: restarts cannot outpace one dispatch cycle.
    throttle = plist["ThrottleInterval"]
    assert isinstance(throttle, int)
    assert throttle >= POLL_INTERVAL_SECONDS, throttle


def test_environment_carries_the_required_vars_and_no_credentials() -> None:
    env = _load()["EnvironmentVariables"]

    # run-project.sh:27 hard-fails without this one.
    assert env["SB_ORCHESTRATOR_CMD"]
    # No shell profile under launchd, so PATH must be explicit and cover the
    # four binaries run-project.sh and the orchestrator shell out to.
    path_dirs = env["PATH"].split(":")
    assert all(Path(d).is_absolute() for d in path_dirs), env["PATH"]

    # Credentials come from ~/.config/switchboard/app.env, never from here.
    assert not [k for k in env if k.startswith("SB_APP_") or k == "GITHUB_TOKEN"]
