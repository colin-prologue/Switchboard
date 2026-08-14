"""Singleton-orchestrator lock (issue #130 / AgDR-036).

Regression guards for the 2026-08-09 two-orchestrator incident: a pre-restart
survivor and its replacement ran concurrently because no launcher held a lock,
double-dispatching every issue.

Holder/prober processes here are PLAIN python subprocesses, not orchestrators —
a real orchestrator would need credentials and a tracker and would strand the
test. What is under test is the flock itself, which is process-scoped and
provider-agnostic.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.scheduler import Orchestrator
from orchestrator.singleton import (
    SingletonLockError,
    acquire_singleton_lock,
    lock_path_for,
)

# Takes the flock, publishes its pid per the protocol, then announces READY.
# The handshake matters: without it a refuser can run before the flock, or
# between the flock and the pid write, and flake either assertion.
HOLDER_SRC = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, str(os.getpid()).encode())
sys.stdout.write("READY\\n")
sys.stdout.flush()
time.sleep(120)
"""

# Exits 3 when the lock is held by someone else, 0 when it acquires.
PROBER_SRC = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(3)
sys.exit(0)
"""


def _spawn_holder(lock: Path) -> subprocess.Popen:
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER_SRC, str(lock)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY"  # flock + pid write done
    return proc


def _probe(lock: Path) -> int:
    return subprocess.run(
        [sys.executable, "-c", PROBER_SRC, str(lock)], timeout=30
    ).returncode


def test_lock_path_is_keyed_on_the_workflow_parent(tmp_path: Path) -> None:
    """Pilot and production self-orchestrators must be mutually exclusive.

    Both WORKFLOW files live in one project directory; keying the lock on the
    filename would give them separate locks and reproduce the incident.
    """
    production = tmp_path / "WORKFLOW.md"
    pilot = tmp_path / "WORKFLOW.pilot-codex.md"

    assert lock_path_for(production) == lock_path_for(pilot)
    assert lock_path_for(production) == tmp_path / ".run" / "orchestrator.lock"


def test_second_launch_refuses_naming_the_lock_path_and_holder_pid(
    tmp_path: Path,
) -> None:
    """A live holder makes a second orchestrator's run() raise before any work.

    The refusal lands on the FIRST statement of run(), so this needs no valid
    workflow, no credentials, no tracker — reaching _load_workflow at all would
    mean the guard sits too late.
    """
    workflow = tmp_path / "WORKFLOW.md"
    lock = lock_path_for(workflow)
    holder = _spawn_holder(lock)
    try:
        orch = Orchestrator(workflow)
        with pytest.raises(SingletonLockError) as excinfo:
            asyncio.run(orch.run())

        message = str(excinfo.value)
        assert str(lock) in message
        assert str(holder.pid) in message
        assert "python child" in message  # not the `uv run` wrapper pid
        assert orch._singleton_lock_fd is None
        assert not workflow.exists()  # never got as far as reading a workflow
    finally:
        holder.kill()
        holder.wait()


def test_holder_pid_falls_back_to_unknown_when_unwritten(tmp_path: Path) -> None:
    """An empty lock file is the normal race window, not an error."""
    workflow = tmp_path / "WORKFLOW.md"
    lock = lock_path_for(workflow)
    holder = _spawn_holder(lock)
    lock.write_text("")  # holder is parked past its pid write; blank it
    try:
        with pytest.raises(SingletonLockError) as excinfo:
            acquire_singleton_lock(workflow)
        assert "holder pid unknown" in str(excinfo.value)
    finally:
        holder.kill()
        holder.wait()


def test_dead_published_pid_is_labelled_stale(tmp_path: Path) -> None:
    """A refuser racing the holder's pid write can read a PREVIOUS holder's
    pid (codex review, PR #139); a dead pid must be labelled stale, never
    reported as the live holder — pid reuse would send the operator to an
    unrelated process."""
    workflow = tmp_path / "WORKFLOW.md"
    lock = lock_path_for(workflow)
    holder = _spawn_holder(lock)
    # a just-reaped child is a genuinely dead pid (reuse within the test
    # window is vanishingly unlikely)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock.write_text(f"{dead.pid}\n")  # simulate the pre-overwrite window
    try:
        with pytest.raises(SingletonLockError) as excinfo:
            acquire_singleton_lock(workflow)
        assert f"{dead.pid} (stale: process dead)" in str(excinfo.value)
        assert "Verify before acting" in str(excinfo.value)
    finally:
        holder.kill()
        holder.wait()


def test_acquired_lock_is_held_against_a_probing_subprocess(tmp_path: Path) -> None:
    """The missing direction: the helper as HOLDER.

    The other tests exercise the real code only as acquirer or refuser, so a
    lock released by refcounting (a builtin `open()` falling out of scope)
    would pass them all while guarding nothing.

    A subprocess prober, not a second in-process acquire: same-process
    different-fd flock denial is a platform detail a regression guard must not
    bet on. No handshake is needed — the acquire completes before the spawn.
    """
    workflow = tmp_path / "WORKFLOW.md"
    fd = acquire_singleton_lock(workflow)
    try:
        assert _probe(lock_path_for(workflow)) == 3  # denied: still held
    finally:
        os.close(fd)


def test_lock_releases_when_the_holder_is_hard_killed(tmp_path: Path) -> None:
    """No staleness heuristics: flock release is tied to process EXIT."""
    workflow = tmp_path / "WORKFLOW.md"
    lock = lock_path_for(workflow)
    holder = _spawn_holder(lock)
    holder.kill()
    holder.wait()  # signal delivery is not exit; wait for the real teardown

    fd = acquire_singleton_lock(workflow)
    try:
        assert lock.read_text().strip() == str(os.getpid())
    finally:
        os.close(fd)


def test_different_workflow_parents_do_not_conflict(tmp_path: Path) -> None:
    """Two projects, two locks.

    Asserted against the helper directly: through run() the success arm would
    proceed into workflow parsing and credentials, and the test would be
    distinguishing outcomes by unrelated exception types.
    """
    first = tmp_path / "project-a" / "WORKFLOW.md"
    second = tmp_path / "project-b" / "WORKFLOW.md"
    first.parent.mkdir()
    second.parent.mkdir()

    fd_a = acquire_singleton_lock(first)
    fd_b = acquire_singleton_lock(second)
    try:
        assert (tmp_path / "project-b" / ".run" / "orchestrator.lock").is_file()
        assert lock_path_for(first) != lock_path_for(second)
    finally:
        os.close(fd_a)
        os.close(fd_b)
