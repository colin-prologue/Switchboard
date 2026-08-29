"""The scheduler tick's runtime-freshness preflight caller (issue #131).

The parent ticket (#32) owns `scripts/freshness-preflight.sh` itself — its
fail-open enumeration, its exit-0 contract, its marker. What is under test here
is exactly what the TICK CALLER does with it: non-blocking and bounded, fail
open on every outcome except cancellation, gated by an interval, ordered before
the reload, fed the right slug, and — above all — inert in a test suite.

Layout note (issue #131 AC): the autouse env scrub lives in `conftest.py`,
which pytest never collects tests from. Every test lives here.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.scheduler import Orchestrator
from orchestrator.types import WorkflowError

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent
PREFLIGHT = REPO_ROOT / "scripts" / "freshness-preflight.sh"
SLUG = "demo"


# --- harness -----------------------------------------------------------------

WORKFLOW_TMPL = """---
tracker:
  kind: github
  repo: "acme/api"
  api_key: "test-token"
  active_states: ["todo", "in progress"]
  terminal_states: ["closed"]
polling:
  interval_ms: 100
workspace:
  root: "{ws_root}"
agent:
  max_concurrent_agents: {agents}
  max_turns: 1
  max_retry_backoff_ms: 500
  max_sessions_per_issue: 2
providers:
  claude:
    kind: claude-cli
    command: "unused-by-fake-tracker"
    max_turns: 1
    turn_timeout_ms: 5000
    read_timeout_ms: 3000
    stall_timeout_ms: 0
{extra}---
prompt body
"""


def workflow_text(ws_root: Path, *, extra: str = "", agents: int = 2) -> str:
    return WORKFLOW_TMPL.format(ws_root=ws_root, extra=extra, agents=agents)


class FakeTracker:
    """Enough tracker for a tick that dispatches nothing. `fetch_calls` is the
    observable for "the tick got past the preflight and reached dispatch"."""

    def __init__(self) -> None:
        self.fetch_calls = 0

    async def fetch_candidate_issues(self):
        self.fetch_calls += 1
        return []

    async def fetch_open_issues(self):
        # issue #52: the tick's one fetch is now the unfiltered form.
        return await self.fetch_candidate_issues()

    def select_candidates(self, issues):
        return list(issues)

    async def fetch_issues_by_states(self, state_names):
        return []

    async def fetch_issue_states_by_ids(self, ids):
        return []

    async def add_issue_comment(self, issue_id, body):
        return None


def build_orchestrator(tmp_path: Path, *, workflow_path: Path | None = None,
                       extra: str = "") -> tuple[Orchestrator, FakeTracker]:
    ws_root = tmp_path / "ws"
    wf = workflow_path if workflow_path is not None else tmp_path / "WORKFLOW.md"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(workflow_text(ws_root, extra=extra))
    orch = Orchestrator(wf)
    orch._load_workflow(initial=True)
    tracker = FakeTracker()
    real_components = orch._components

    def fake_components():
        _, wsm = real_components()
        return tracker, wsm

    orch._components = fake_components
    return orch, tracker


def write_stub_preflight(sb_home: Path, body: str) -> Path:
    """Install a stub at the ABSOLUTE path the tick execs."""
    script = sb_home / "scripts" / "freshness-preflight.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\n" + body)
    return script


def bind_env(monkeypatch, sb_home: Path, slug: str = SLUG) -> None:
    monkeypatch.setenv("SB_HOME", str(sb_home))
    monkeypatch.setenv("SB_PROJECT_SLUG", slug)


# --- skip rule ----------------------------------------------------------------


async def test_unbound_env_skips_the_preflight_entirely(tmp_path, capsys):
    """The env-less local run: the autouse scrub leaves all three unbound, so
    the tick skips with one named warning and never execs anything."""
    orch, tracker = build_orchestrator(tmp_path)
    await orch._tick()
    err = capsys.readouterr().err
    assert "freshness preflight skipped" in err
    assert tracker.fetch_calls == 1  # the tick proceeded


async def test_empty_env_var_skips_too(tmp_path, monkeypatch, capsys):
    """`SB_HOME=""` is BOUND but degrades the script to a `/scripts/...` path —
    falsiness, not membership, is the test."""
    monkeypatch.setenv("SB_HOME", "")
    monkeypatch.setenv("SB_PROJECT_SLUG", SLUG)
    orch, _ = build_orchestrator(tmp_path)
    await orch._tick()
    assert "freshness preflight skipped" in capsys.readouterr().err


async def test_skip_does_not_stamp_the_interval(tmp_path, monkeypatch, capsys):
    """A skip never execs, so it must not consume the interval: binding the
    env after a skipped tick still execs on the very next one."""
    sb_home = tmp_path / "sb"
    marker = tmp_path / "ran"
    write_stub_preflight(sb_home, f'echo "$1" >> "{marker}"\n')
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()  # unbound -> skip
    assert orch._freshness_last_run_at is None
    bind_env(monkeypatch, sb_home)
    await orch._tick()
    assert marker.read_text().split() == [SLUG]


# --- caller contract: every fail-open outcome lets the tick proceed ------------


async def test_non_zero_exit_does_not_abort_the_tick(tmp_path, monkeypatch, capsys):
    sb_home = tmp_path / "sb"
    write_stub_preflight(sb_home, 'echo "boom" >&2\nexit 3\n')
    bind_env(monkeypatch, sb_home)
    orch, tracker = build_orchestrator(tmp_path)

    await orch._tick()

    err = capsys.readouterr().err
    assert "freshness preflight exited non-zero" in err
    assert "boom" in err  # streams re-logged
    assert tracker.fetch_calls == 1  # dispatch was reached in the SAME tick


async def test_missing_script_does_not_abort_the_tick(tmp_path, monkeypatch, capsys):
    """An `$SB_HOME` with no preflight installed: `bash` exits 127, which is
    the non-zero-exit fail-open, not a spawn failure."""
    bind_env(monkeypatch, tmp_path / "no-such-home")
    orch, tracker = build_orchestrator(tmp_path)

    await orch._tick()

    assert "freshness preflight exited non-zero" in capsys.readouterr().err
    assert tracker.fetch_calls == 1


async def test_spawn_failure_does_not_abort_the_tick(tmp_path, monkeypatch, capsys):
    """The genuine spawn failure — the exec itself raises (no `bash`, fork
    limit, a permission wall). It must not reach the blanket handler."""
    sb_home = tmp_path / "sb"
    write_stub_preflight(sb_home, "exit 0\n")
    bind_env(monkeypatch, sb_home)
    orch, tracker = build_orchestrator(tmp_path)

    async def boom(*args, **kwargs):
        raise OSError("cannot fork")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    await orch._tick()

    assert "freshness preflight could not be run" in capsys.readouterr().err
    assert tracker.fetch_calls == 1


async def test_hung_preflight_is_bounded_and_group_killed(tmp_path, monkeypatch, capsys):
    """A hung preflight must not delay the tick past `freshness.timeout_ms`,
    and its backgrounded children must not outlive the await either."""
    sb_home = tmp_path / "sb"
    pidfile = tmp_path / "childpid"
    write_stub_preflight(sb_home, f'sleep 120 &\necho $! > "{pidfile}"\nwait\n')
    bind_env(monkeypatch, sb_home)
    orch, tracker = build_orchestrator(
        tmp_path, extra="freshness:\n  timeout_ms: 400\n")

    started = time.monotonic()
    await orch._tick()
    elapsed = time.monotonic() - started

    assert elapsed < 20, f"tick outlived the bound: {elapsed:.1f}s"
    assert "freshness preflight timed out" in capsys.readouterr().err
    assert tracker.fetch_calls == 1

    child_pid = int(pidfile.read_text().strip())
    deadline = time.monotonic() + 5
    while True:
        try:
            os.kill(child_pid, 0)
        except (ProcessLookupError, PermissionError):
            break  # reaped or no longer ours
        if time.monotonic() > deadline:
            raise AssertionError(f"backgrounded child {child_pid} survived the kill")
        time.sleep(0.05)


async def test_cancellation_kills_and_re_raises(tmp_path, monkeypatch, capsys):
    """`CancelledError` is a `BaseException` on 3.12 — the blanket `except
    Exception` in `run()` never sees it, and swallowing it here would defeat
    cooperative shutdown on exactly the tick that is mid-preflight."""
    sb_home = tmp_path / "sb"
    write_stub_preflight(sb_home, "sleep 120\n")
    bind_env(monkeypatch, sb_home)
    orch, _ = build_orchestrator(tmp_path)

    task = asyncio.create_task(orch._tick())
    await asyncio.sleep(0.4)  # let the subprocess actually start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "freshness preflight cancelled" in capsys.readouterr().err


# --- ordering: same-tick adoption of a recompose -------------------------------


async def test_recompose_is_adopted_in_the_same_tick(tmp_path, monkeypatch):
    """The preflight is awaited immediately BEFORE `_maybe_reload()`.

    The orchestrator is constructed on `$SB_HOME/.run/<slug>/composed-WORKFLOW.md`
    — the launch path's own `--workflow` argument. Without that coupling the
    assertion is satisfiable by any tmp file the preflight rewrites, and proves
    only that `_maybe_reload` notices an mtime bump.
    """
    sb_home = tmp_path / "sb"
    composed = sb_home / ".run" / SLUG / "composed-WORKFLOW.md"
    orch, tracker = build_orchestrator(tmp_path, workflow_path=composed)
    assert orch._cfg.agent().max_concurrent_agents == 2
    # Push the loaded mtime back so the rewrite is unambiguously newer.
    old = composed.stat().st_mtime - 10
    os.utime(composed, (old, old))
    orch._workflow_mtime = composed.stat().st_mtime

    recomposed = workflow_text(tmp_path / "ws", agents=7)
    write_stub_preflight(
        sb_home,
        f'cat > "{composed}" <<\'SB_EOF\'\n{recomposed}SB_EOF\n',
    )
    bind_env(monkeypatch, sb_home)

    await orch._tick()

    assert orch._cfg.agent().max_concurrent_agents == 7
    assert tracker.fetch_calls == 1  # …and dispatch ran on the NEW config


# --- inputs: the slug, and the absolute script path ----------------------------


async def test_slug_comes_from_sb_project_slug(tmp_path, monkeypatch):
    sb_home = tmp_path / "sb"
    argfile = tmp_path / "arg"
    write_stub_preflight(sb_home, f'echo "$1" > "{argfile}"\n')
    bind_env(monkeypatch, sb_home, slug="codex-canary")
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()

    assert argfile.read_text().strip() == "codex-canary"


async def test_script_is_execed_by_absolute_path_not_via_cwd(tmp_path, monkeypatch):
    """A decoy on the CWD-relative path must never win."""
    sb_home = tmp_path / "sb"
    real = tmp_path / "real"
    write_stub_preflight(sb_home, f'echo real > "{real}"\n')
    decoy_home = tmp_path / "cwd"
    write_stub_preflight(decoy_home, f'echo decoy > "{real}"\n')
    monkeypatch.chdir(decoy_home)
    bind_env(monkeypatch, sb_home)
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()

    assert real.read_text().strip() == "real"


# --- cadence -------------------------------------------------------------------


async def test_min_interval_gates_the_exec(tmp_path, monkeypatch):
    """Default cadence: the first eligible tick execs (last-run starts UNSET),
    the next one inside the window does not."""
    sb_home = tmp_path / "sb"
    counter = tmp_path / "runs"
    write_stub_preflight(sb_home, f'echo x >> "{counter}"\n')
    bind_env(monkeypatch, sb_home)
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()
    await orch._tick()
    await orch._tick()

    assert len(counter.read_text().split()) == 1


async def test_min_interval_key_is_read_from_config(tmp_path, monkeypatch):
    """The key GATES the fetch: shrink the window and the second tick execs."""
    sb_home = tmp_path / "sb"
    counter = tmp_path / "runs"
    write_stub_preflight(sb_home, f'echo x >> "{counter}"\n')
    bind_env(monkeypatch, sb_home)
    orch, _ = build_orchestrator(
        tmp_path, extra="freshness:\n  min_interval_ms: 1\n")

    await orch._tick()
    await orch._tick()

    assert len(counter.read_text().split()) == 2


async def test_timeout_consumes_the_interval(tmp_path, monkeypatch):
    """Stamped AT EXEC: a hanging preflight must not re-exec on every tick, or
    "one long tick in ten" becomes a permanently long cycle."""
    sb_home = tmp_path / "sb"
    counter = tmp_path / "runs"
    write_stub_preflight(sb_home, f'echo x >> "{counter}"\nsleep 120\n')
    bind_env(monkeypatch, sb_home)
    orch, _ = build_orchestrator(
        tmp_path, extra="freshness:\n  timeout_ms: 300\n")

    await orch._tick()
    await orch._tick()

    assert len(counter.read_text().split()) == 1


def test_invalid_freshness_timeout_fails_the_initial_load(tmp_path):
    """Out-of-range values fail at LOAD (via `validate_dispatch`'s force-call
    block), never per-tick inside the blanket handler."""
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(workflow_text(tmp_path / "ws",
                                extra="freshness:\n  timeout_ms: 0\n"))
    orch = Orchestrator(wf)
    with pytest.raises(WorkflowError) as exc:
        orch._load_workflow(initial=True)
    assert "freshness.timeout_ms" in str(exc.value)


def test_invalid_freshness_min_interval_fails_the_initial_load(tmp_path):
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(workflow_text(tmp_path / "ws",
                                extra="freshness:\n  min_interval_ms: -5\n"))
    orch = Orchestrator(wf)
    with pytest.raises(WorkflowError) as exc:
        orch._load_workflow(initial=True)
    assert "freshness.min_interval_ms" in str(exc.value)


# --- SB_LAUNCH_SHA pass-through -------------------------------------------------


async def test_launch_sha_is_passed_through_unchanged(tmp_path, monkeypatch):
    sb_home = tmp_path / "sb"
    out = tmp_path / "sha"
    write_stub_preflight(sb_home, f'echo "${{SB_LAUNCH_SHA:-<unset>}}" > "{out}"\n')
    bind_env(monkeypatch, sb_home)
    monkeypatch.setenv("SB_LAUNCH_SHA", "0123456789abcdef0123456789abcdef01234567")
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()

    assert out.read_text().strip() == "0123456789abcdef0123456789abcdef01234567"


async def test_unbound_launch_sha_is_never_synthesized(tmp_path, monkeypatch):
    """The tick must not hand the script a HEAD-derived substitute: that is the
    silent degradation #32 removed."""
    sb_home = tmp_path / "sb"
    out = tmp_path / "sha"
    write_stub_preflight(sb_home, f'echo "${{SB_LAUNCH_SHA:-<unset>}}" > "{out}"\n')
    bind_env(monkeypatch, sb_home)  # the autouse scrub left SB_LAUNCH_SHA unbound
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()

    assert out.read_text().strip() == "<unset>"


# --- real-script smoke ----------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
                   cwd=str(cwd), check=True, capture_output=True, text=True)


def build_sb_home_skeleton(tmp_path: Path, slug: str = SLUG) -> Path:
    """A minimal but REAL `$SB_HOME`: the copied preflight, the seed-source
    tracked workflow, and a repo with a bare origin.

    The repo is not optional — without it the script fail-opens at
    missing-repo before the loaded-sha branch can run, and the sha assertions
    below would be green for the wrong reason. (#32's own `sb_home` fixture is
    module-local to `test_run_project_script.py` and not importable.)
    """
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, home / "scripts" / "freshness-preflight.sh")

    project = home / "projects" / slug
    project.mkdir(parents=True)
    (project / "project.env").write_text(
        f"SB_PROJECT_SLUG={slug}\nSB_GITHUB_REPO=acme/widgets\n"
        f"SB_BASE_BRANCH=main\nSB_WORKSPACE_ROOT={tmp_path / 'ws'}\n")
    (project / "WORKFLOW.md").write_text(
        "---\npool:\n  max_concurrent_agents: 2\n---\nprompt")

    (home / "workflow").mkdir()
    (home / "workflow" / "WORKFLOW.base.md").write_text(
        "---\nrepo: {{REPO}}\nroot: {{WORKSPACE_ROOT}}\nagents: {{MAX_AGENTS}}\n"
        "---\nbody\n")
    (home / "orchestrator" / "src").mkdir(parents=True)
    (home / "orchestrator" / "src" / "mod.py").write_text("v1\n")
    (home / ".gitignore").write_text(".run/\n")

    _git(home, "init", "-b", "main", "-q")
    _git(home, "add", "-A")
    _git(home, "commit", "-qm", "skeleton")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", "-q", str(bare))
    _git(home, "remote", "add", "origin", str(bare))
    _git(home, "push", "-q", "origin", "main")
    return home


async def test_real_preflight_smoke(tmp_path, monkeypatch, capsys):
    """The tick execs the REAL script against a real skeleton.

    `rc == 0` alone is green in both drift directions that matter — #32's
    exit-0-on-every-fail-open contract makes a wrong-but-well-formed slug and
    an unforwarded sha both exit 0 — so the observables are the composed path
    and the script's own warning line.
    """
    sb_home = build_sb_home_skeleton(tmp_path)
    bind_env(monkeypatch, sb_home)
    orch, tracker = build_orchestrator(tmp_path)

    await orch._tick()

    err = capsys.readouterr().err
    # (iii) spawn/usage guard: neither fail-open branch of the CALLER fired.
    assert "freshness preflight could not be run" not in err
    assert "freshness preflight exited non-zero" not in err
    assert "freshness preflight timed out" not in err

    # (i) the wrong-slug row: output lands under the slug the tick was given,
    # and no other project's .run/ directory is touched.
    composed = sb_home / ".run" / SLUG / "composed-WORKFLOW.md"
    assert composed.exists(), err
    assert "repo: acme/widgets" in composed.read_text()
    assert [p.name for p in (sb_home / ".run").iterdir()] == [SLUG]

    # (ii, negative direction) with SB_LAUNCH_SHA unbound the script's named
    # degradation warning IS present in the re-logged stderr — which pins the
    # substring the positive direction asserts the absence of.
    assert "SB_LAUNCH_SHA unbound" in err
    assert tracker.fetch_calls == 1


async def test_real_preflight_forwards_launch_sha(tmp_path, monkeypatch, capsys):
    """(ii, positive direction) bound sha -> no degradation warning."""
    sb_home = build_sb_home_skeleton(tmp_path)
    head = subprocess.run(["git", "-C", str(sb_home), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    bind_env(monkeypatch, sb_home)
    monkeypatch.setenv("SB_LAUNCH_SHA", head)
    orch, _ = build_orchestrator(tmp_path)

    await orch._tick()

    err = capsys.readouterr().err
    assert "SB_LAUNCH_SHA unbound" not in err
    assert (sb_home / ".run" / SLUG / "composed-WORKFLOW.md").exists()


# --- suite hermeticity (the regression test the scrub exists for) ---------------


def _snapshot(path: Path):
    """mtime + content, tolerating absence.

    ABSENT is a legitimate value: a fresh clone has no `FETCH_HEAD` at all, so
    absent -> present IS the leak signal, not an error.
    """
    try:
        stat = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return (stat.st_mtime_ns, path.read_bytes() if path.is_file() else b"")


@pytest.mark.parametrize("slug", ["switchboard-self"])
def test_suite_is_hermetic_under_an_ambiently_bound_sb_home(slug):
    """A dispatched worker inherits a bound `SB_HOME`, so the tick's own skip
    rule would NOT fire there — the `conftest.py` scrub is the mechanism, and
    this is its regression test.

    A child pytest runs a real tick-bearing module with all three variables
    bound at the REAL repo (a bogus path would pass for the wrong reason:
    nothing could exec). If the scrub broke, those ticks would fetch and
    recompose the production orchestrator's watched config.
    """
    watched = [
        REPO_ROOT / ".run" / slug / "composed-WORKFLOW.md",
        REPO_ROOT / ".run" / slug / "restart-needed.json",
        REPO_ROOT / ".git" / "FETCH_HEAD",
    ]
    before = {p: _snapshot(p) for p in watched}
    status_before = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        capture_output=True, text=True).stdout

    child = subprocess.run(
        [sys.executable, "-m", "pytest",
         str(TESTS_DIR / "test_dispatch_guard.py"), "-q"],
        cwd=str(REPO_ROOT),
        # `{**os.environ, ...}`, never a bare three-key dict: stripping
        # PATH/venv would make the child fail to start and pass the
        # assertions for the wrong reason.
        env={**os.environ, "SB_HOME": str(REPO_ROOT),
             "SB_PROJECT_SLUG": slug,
             "SB_LAUNCH_SHA": "0" * 40},
        capture_output=True, text=True)

    # Liveness: a collection error would exit non-zero having run no tick,
    # leaving every snapshot unmoved and this test green while testing nothing.
    assert child.returncode == 0, child.stdout[-4000:] + child.stderr[-4000:]

    after = {p: _snapshot(p) for p in watched}
    for path in watched:
        assert after[path] == before[path], (
            f"{path} moved during a child suite run — the env scrub leaked")
    status_after = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
        capture_output=True, text=True).stdout
    assert status_after == status_before
