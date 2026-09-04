"""Tests for the external fleet-health observer (issue #193).

Every fixture builds its own fake fleet under `tmp_path` — a repo root with
`projects/<slug>/project.env`, a log directory, and a LaunchAgents directory —
so nothing here reads the real machine. `launchctl` is injected rather than
invoked, which is what lets the whole detection surface run on a box with no
launchd at all.

The slugs are made up. `switchboard-self` is the only project in this
repository and no criterion here asserts anything about it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.fleet_health import (
    EXIT_DEGRADED,
    EXIT_OK,
    LEVEL_DEGRADED,
    LEVEL_NOTICE,
    PRECEDENCE,
    STATE_CRASH_LOOP,
    STATE_DOWN,
    STATE_OK,
    STATE_STALE_CODE,
    STATE_UNKNOWN,
    STATE_WEDGED,
    JobState,
    Observation,
    Paths,
    Thresholds,
    Window,
    build_report,
    check_fleet,
    check_slug,
    discover_slugs,
    load_state,
    main,
    read_log,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE = REPO_ROOT / "orchestrator" / "src" / "orchestrator" / "fleet_health.py"
WRAPPER = REPO_ROOT / "scripts" / "fleet-health.sh"
SETUP_MD = REPO_ROOT / "SETUP.md"

SLUG = "acme-widgets"
NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

BANNER = "[run-project] acme-widgets -> acme/widgets (workspaces: /tmp/ws)"
STARTED = (
    '2026-09-03T08:00:00Z orchestrator starting workflow=/tmp/WORKFLOW.md '
    'repo=acme/widgets workspace_root=/tmp/ws sha={sha} dirty=false'
)
TICK_ERROR = (
    '2026-09-03T11:{mm:02d}:00Z tick error error="RuntimeError(\'boom\')"'
)
# The substring the SETUP recipe over-matched: a HANDLED in-tick failure. It
# contains "tick error" and must not be counted.
HANDLED = (
    '2026-09-03T11:05:00Z candidate fetch failed; skipping dispatch this tick '
    'error="transport error: ReadTimeout"'
)


def _fleet(tmp_path: Path, slug: str = SLUG) -> Paths:
    paths = Paths(
        repo_root=tmp_path / "repo",
        log_dir=tmp_path / "logs",
        launch_agents_dir=tmp_path / "agents",
    )
    (paths.repo_root / "projects" / slug).mkdir(parents=True)
    (paths.repo_root / "projects" / slug / "project.env").write_text(
        f"SB_GITHUB_REPO=acme/{slug}\n", encoding="utf-8")
    paths.log_dir.mkdir(parents=True)
    paths.launch_agents_dir.mkdir(parents=True)
    return paths


def _write_log(paths: Paths, lines: list[str], slug: str = SLUG) -> None:
    (paths.log_dir / f"{slug}.log").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def _install_plist(paths: Paths, slug: str = SLUG) -> Path:
    plist = paths.launch_agents_dir / f"com.switchboard.{slug}.plist"
    plist.write_text("<plist/>\n", encoding="utf-8")
    return plist


def _marker(paths: Paths, *, written_at: datetime, target: str = "a" * 40,
            loaded: str = "b" * 40, behind: int = 3, slug: str = SLUG) -> Path:
    d = paths.repo_root / ".run" / slug
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "restart-needed.json"
    marker.write_text(json.dumps({
        "target_sha": target,
        "loaded_sha": loaded,
        "loaded_sha_source": "SB_LAUNCH_SHA",
        "behind_commits": behind,
        "written_at": written_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }), encoding="utf-8")
    return marker


def _obs(*, minutes_ago: float, banners: int, tick_errors: int,
         log_bytes: int = 0, window: Window | None = None) -> Observation:
    return Observation(
        observed_at=NOW - timedelta(minutes=minutes_ago),
        banners=banners, tick_errors=tick_errors, log_bytes=log_bytes,
        window=window,
    )


RUNNING = JobState(known=True, loaded=True, running=True, evidence="PID 1")
STOPPED = JobState(known=True, loaded=True, running=False,
                   evidence="com.switchboard.acme-widgets loaded, no PID")
UNLOADED = JobState(known=True, loaded=False, running=False,
                    evidence="exit 113")
UNQUERYABLE = JobState(known=False, evidence="launchctl not on PATH")


def _states(result) -> set[str]:
    return {f.state for f in result.findings}


# --- the external-observer invariant -----------------------------------------

def test_module_makes_no_network_or_model_calls():
    """The invariant is structural, so it is checked structurally: a health
    signal that reaches the network can be broken by the same outage it is
    meant to explain."""
    source = MODULE.read_text(encoding="utf-8")
    for forbidden in ("httpx", "urllib", "socket", "requests", "http.client",
                      "agent_runner", "tracker", "scheduler"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source
    # `.log` is the one in-tree import, and it is stdlib-only itself.
    assert "from .log import log" in source


def test_reports_a_complete_fleet_with_every_process_dead(tmp_path):
    """No logs, no markers, no launchd: still a complete report, exit 0, and a
    named finding per slug rather than an error."""
    paths = _fleet(tmp_path)
    results = check_fleet([SLUG], paths, Thresholds(), {}, NOW,
                          probe=lambda s: UNQUERYABLE)
    assert [r.slug for r in results] == [SLUG]
    assert results[0].state == STATE_UNKNOWN
    assert not results[0].degraded
    assert "no log at" in results[0].findings[0].evidence


def test_discover_slugs_unions_bindings_and_installed_plists(tmp_path):
    paths = _fleet(tmp_path)
    _install_plist(paths, "other-project")
    assert discover_slugs(paths) == ["acme-widgets", "other-project"]


# --- (a) crash loop ----------------------------------------------------------

def test_crash_loop_is_growth_not_a_bare_count(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] * 12)
    prev = _obs(minutes_ago=10, banners=8, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert result.state == STATE_CRASH_LOOP
    finding = next(f for f in result.findings if f.state == STATE_CRASH_LOOP)
    assert finding.level == LEVEL_DEGRADED
    assert "4 new startup banners" in finding.evidence
    assert BANNER in finding.evidence
    assert "unload" in finding.remedy


def test_a_large_banner_count_that_is_not_growing_is_not_a_crash_loop(tmp_path):
    """Ninety restarts over a year is a long-lived fleet, not a loop."""
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] * 90)
    prev = _obs(minutes_ago=10, banners=90, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert STATE_CRASH_LOOP not in _states(result)


def test_one_intentional_restart_is_below_the_threshold(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] * 3)
    prev = _obs(minutes_ago=10, banners=2, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert STATE_CRASH_LOOP not in _states(result)


# --- (b) wedged ticks --------------------------------------------------------

def test_tick_error_match_is_the_record_name_not_a_substring(tmp_path):
    """The 94-to-0 over-count: handled in-tick failures carry the substring."""
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] + [HANDLED] * 94)
    facts = read_log(paths.log_dir / f"{SLUG}.log", SLUG)
    assert facts.tick_errors == 0
    assert facts.banners == 1


def test_wedged_when_tick_errors_grow_at_the_tick_cadence(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] + [TICK_ERROR.format(mm=i % 60) for i in range(20)])
    prev = _obs(minutes_ago=10, banners=1, tick_errors=0)  # 600s -> ~20 ticks
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert result.state == STATE_WEDGED
    finding = next(f for f in result.findings if f.state == STATE_WEDGED)
    assert "20 new tick-error records" in finding.evidence
    assert "tick error" in finding.evidence          # the record itself
    assert "launchctl stop" in finding.remedy and "launchctl start" in finding.remedy
    assert "THEN" in finding.remedy                  # stop alone leaves it DOWN


def test_a_few_transport_flakes_are_a_notice_not_wedged(tmp_path):
    """Several ReadTimeouts a day is production, not a wedge."""
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] + [TICK_ERROR.format(mm=i) for i in range(3)])
    prev = _obs(minutes_ago=10, banners=1, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert STATE_WEDGED not in _states(result)
    assert not result.degraded
    notice = next(f for f in result.findings if f.level == LEVEL_NOTICE)
    assert "transient flakiness" in notice.evidence


def test_wedged_is_suppressed_when_the_process_is_down(tmp_path):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER] + [TICK_ERROR.format(mm=i % 60) for i in range(20)])
    prev = _obs(minutes_ago=10, banners=1, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, STOPPED)
    assert STATE_WEDGED not in _states(result)
    assert result.state == STATE_DOWN


def test_wedged_is_suppressed_when_the_banner_also_grew(tmp_path):
    """Tick errors plus restarts is a crash loop; (a) owns the louder story."""
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] * 5 + [TICK_ERROR.format(mm=i % 60) for i in range(20)])
    prev = _obs(minutes_ago=10, banners=1, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert STATE_WEDGED not in _states(result)
    assert result.state == STATE_CRASH_LOOP


def test_no_previous_observation_yields_no_rate_finding(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER] * 40 + [TICK_ERROR.format(mm=i % 60) for i in range(40)])
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert STATE_WEDGED not in _states(result)
    assert STATE_CRASH_LOOP not in _states(result)
    assert result.notes == ("first observation — no rate available",)


def test_a_truncated_log_resets_the_baseline_instead_of_going_negative(tmp_path):
    """#12 owns rotation; this check only has to survive it. A negative delta
    must not read as a clean fleet."""
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER])
    prev = _obs(minutes_ago=10, banners=40, tick_errors=900, log_bytes=999_999)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert result.notes == ("log truncated or rotated — baseline reset",)
    assert result.observation.banners == 1
    assert result.observation.window is None


# --- (c) stale code ----------------------------------------------------------

def test_an_old_marker_is_stale_code(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER])
    _marker(paths, written_at=NOW - timedelta(hours=9))
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert result.state == STATE_STALE_CODE
    finding = next(f for f in result.findings if f.state == STATE_STALE_CODE)
    assert "behind_commits=3" in finding.evidence
    assert "9.0h old" in finding.evidence
    assert "launchctl stop" in finding.remedy


def test_a_fresh_marker_is_a_notice_because_the_preflight_already_showed_it(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER])
    _marker(paths, written_at=NOW - timedelta(hours=1))
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert STATE_STALE_CODE not in _states(result)
    assert not result.degraded


def test_startup_identity_matching_the_target_supersedes_marker_age(tmp_path):
    """The direct signal beats the marker: the process states which build it
    loaded, and a marker the operator already acted on is not staleness."""
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER, STARTED.format(sha="a" * 40)])
    _marker(paths, written_at=NOW - timedelta(hours=9), target="a" * 40)
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert STATE_STALE_CODE not in _states(result)
    assert not result.degraded
    assert any("superseded" in f.evidence for f in result.findings)


def test_startup_identity_behind_the_target_is_stale_code_at_any_marker_age(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER, STARTED.format(sha="b" * 40)])
    _marker(paths, written_at=NOW - timedelta(minutes=5), target="a" * 40)
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert result.state == STATE_STALE_CODE
    finding = next(f for f in result.findings if f.state == STATE_STALE_CODE)
    assert "the running process states sha=bbbbbbbbbbbb" in finding.evidence


def test_unknown_startup_sha_degrades_to_marker_age_not_to_an_error(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER, STARTED.format(sha="unknown")])
    _marker(paths, written_at=NOW - timedelta(hours=9))
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert result.state == STATE_STALE_CODE
    assert "9.0h old" in next(
        f for f in result.findings if f.state == STATE_STALE_CODE).evidence


def test_no_marker_is_no_stale_code_finding(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER])
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, RUNNING)
    assert STATE_STALE_CODE not in _states(result)


# --- (d) process down --------------------------------------------------------

def test_loaded_but_not_running_is_down_with_the_start_remedy(tmp_path):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER])
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, STOPPED)
    assert result.state == STATE_DOWN
    finding = next(f for f in result.findings if f.state == STATE_DOWN)
    assert finding.remedy == f"launchctl start com.switchboard.{SLUG}"
    assert "successful exit" in finding.evidence


def test_installed_but_not_loaded_is_down_with_the_load_remedy(tmp_path):
    paths = _fleet(tmp_path)
    plist = _install_plist(paths)
    _write_log(paths, [BANNER])
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, UNLOADED)
    assert result.state == STATE_DOWN
    finding = next(f for f in result.findings if f.state == STATE_DOWN)
    assert finding.remedy == f"launchctl load {plist}"


def test_no_plist_means_no_expectation_and_no_down_finding(tmp_path):
    paths = _fleet(tmp_path)
    _write_log(paths, [BANNER])
    result = check_slug(SLUG, paths, Thresholds(), None, NOW, UNLOADED)
    assert STATE_DOWN not in _states(result)


def test_a_running_supervised_slug_with_a_quiet_log_is_ok(tmp_path):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER, STARTED.format(sha="a" * 40)])
    prev = _obs(minutes_ago=10, banners=1, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, RUNNING)
    assert result.state == STATE_OK
    assert result.findings == ()


# --- precedence and the report -----------------------------------------------

def test_precedence_is_declared_and_down_outranks_everything(tmp_path):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER] * 9)
    _marker(paths, written_at=NOW - timedelta(hours=9))
    prev = _obs(minutes_ago=10, banners=1, tick_errors=0)
    result = check_slug(SLUG, paths, Thresholds(), prev, NOW, STOPPED)
    assert _states(result) >= {STATE_DOWN, STATE_CRASH_LOOP, STATE_STALE_CODE}
    assert result.state == STATE_DOWN
    assert PRECEDENCE.index(STATE_DOWN) < PRECEDENCE.index(STATE_CRASH_LOOP)
    assert PRECEDENCE.index(STATE_UNKNOWN) < PRECEDENCE.index(STATE_OK)


def test_report_carries_state_evidence_and_remedy_per_slug(tmp_path):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER])
    results = check_fleet([SLUG], paths, Thresholds(), {}, NOW,
                          probe=lambda s: STOPPED)
    report = build_report(results, NOW)
    assert report["generated_at"] == "2026-09-03T12:00:00Z"
    assert report["degraded"] is True
    assert report["degraded_slugs"] == [SLUG]
    entry = report["slugs"][SLUG]
    assert entry["state"] == STATE_DOWN
    assert entry["findings"][0]["level"] == LEVEL_DEGRADED
    assert entry["findings"][0]["remedy"]
    assert entry["observation"]["banners"] == 1


def test_corrupt_state_degrades_to_no_rate_never_to_a_crash(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_state(bad) == {}
    assert load_state(tmp_path / "missing.json") == {}


# --- the CLI: exit codes, idempotence, read-only ------------------------------

def _run_main(paths: Paths, *extra: str) -> int:
    return main([
        "--repo-root", str(paths.repo_root),
        "--log-dir", str(paths.log_dir),
        "--launch-agents-dir", str(paths.launch_agents_dir),
        *extra,
    ])


def test_exit_code_distinguishes_all_ok_from_any_degraded(tmp_path, monkeypatch):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER])
    monkeypatch.setattr("orchestrator.fleet_health.probe_launchctl",
                        lambda slug, **kw: RUNNING)
    assert _run_main(paths) == EXIT_OK
    monkeypatch.setattr("orchestrator.fleet_health.probe_launchctl",
                        lambda slug, **kw: STOPPED)
    assert _run_main(paths) == EXIT_DEGRADED


def test_running_it_twice_back_to_back_yields_the_same_states(tmp_path, monkeypatch):
    """The rate detections must not be self-triggering: the second run's window
    is seconds long, and advancing the baseline on the first run would have made
    a wedged fleet look recovered."""
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER] + [TICK_ERROR.format(mm=i % 60) for i in range(20)])
    monkeypatch.setattr("orchestrator.fleet_health.probe_launchctl",
                        lambda slug, **kw: RUNNING)
    # Seed a baseline ten minutes old so the first run has a real window.
    _write_json = paths.state
    _write_json.parent.mkdir(parents=True, exist_ok=True)
    _write_json.write_text(json.dumps({
        "version": 1,
        "slugs": {SLUG: {
            "observed_at": (datetime.now(timezone.utc)
                            - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "banners": 1, "tick_errors": 0, "log_bytes": 0, "window": None,
        }},
    }), encoding="utf-8")

    first = _run_main(paths)
    first_report = json.loads(paths.report.read_text(encoding="utf-8"))
    second = _run_main(paths)
    second_report = json.loads(paths.report.read_text(encoding="utf-8"))

    assert first == second == EXIT_DEGRADED
    assert first_report["slugs"][SLUG]["state"] == STATE_WEDGED
    assert second_report["slugs"][SLUG]["state"] == STATE_WEDGED
    assert ({s: v["state"] for s, v in first_report["slugs"].items()}
            == {s: v["state"] for s, v in second_report["slugs"].items()})


def test_the_check_mutates_nothing_but_its_own_two_files(tmp_path, monkeypatch):
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER])
    marker = _marker(paths, written_at=NOW - timedelta(hours=9))
    monkeypatch.setattr("orchestrator.fleet_health.probe_launchctl",
                        lambda slug, **kw: RUNNING)

    def snapshot(root: Path) -> dict[str, tuple[int, bytes]]:
        return {
            str(p.relative_to(root)): (p.stat().st_size, p.read_bytes())
            for p in sorted(root.rglob("*")) if p.is_file()
        }

    before = snapshot(tmp_path)
    assert _run_main(paths) == EXIT_DEGRADED
    after = snapshot(tmp_path)

    new = set(after) - set(before)
    assert new == {
        str(paths.report.relative_to(tmp_path)),
        str(paths.state.relative_to(tmp_path)),
    }
    assert {k: v for k, v in after.items() if k in before} == before
    assert marker.read_text(encoding="utf-8")  # untouched, and still readable


def test_a_fleet_with_no_slugs_at_all_exits_clean(tmp_path):
    paths = Paths(repo_root=tmp_path / "empty",
                  log_dir=tmp_path / "logs",
                  launch_agents_dir=tmp_path / "agents")
    assert _run_main(paths) == EXIT_OK


# --- the wiring: the wrapper the interval job actually runs -------------------

@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_wrapper_script_runs_the_check_with_a_bare_python3(tmp_path):
    """The wrapper is the thing cron/launchd invokes. A pytest-only check would
    itself be a shipped feature that never runs — and the wrapper's whole point
    is that it works without `uv` and without a project virtualenv."""
    paths = _fleet(tmp_path)
    _install_plist(paths)
    _write_log(paths, [BANNER])
    proc = subprocess.run(
        ["bash", str(WRAPPER),
         "--repo-root", str(paths.repo_root),
         "--log-dir", str(paths.log_dir),
         "--launch-agents-dir", str(paths.launch_agents_dir),
         SLUG],
        capture_output=True, timeout=60,
        env={**os.environ, "PYTHONPATH": ""},
    )
    assert proc.returncode in (EXIT_OK, EXIT_DEGRADED), proc.stderr.decode()
    assert b"fleet health" in proc.stderr
    assert paths.report.is_file()
    report = json.loads(paths.report.read_text(encoding="utf-8"))
    assert SLUG in report["slugs"]


# --- the documentation the ticket also owns ----------------------------------

def test_setup_points_at_the_check_and_the_two_triaged_defects_are_fixed():
    setup = SETUP_MD.read_text(encoding="utf-8")
    assert "scripts/fleet-health.sh" in setup
    # The stale citation triage found: the tick-swallow moved long ago.
    assert "scheduler.py:450-451" not in setup
    # The over-matching recipe: a bare substring grep counts handled
    # `…this tick error=` lines. The documented recipe must anchor.
    assert "grep -c 'tick error'" not in setup
