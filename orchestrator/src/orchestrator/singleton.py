"""One orchestrator process per project checkout (issue #130 / AgDR-036).

On 2026-08-09 two orchestrator processes ran concurrently against
`switchboard-self`: a pre-#128 survivor that outlived its restart, plus its
replacement. Nothing held a lock — no launcher does — so every dispatch
happened twice: paired verify verdicts seconds apart, per-role budgets burning
at 2x, mixed park-message formats, sticky re-parks.

The guard is an `fcntl.flock` held by the orchestrator PROCESS, not by the
launching shell: an orphaned survivor still holds it, and a fresh launch fails
loudly naming the real holder. `flock` releases on process death (including
`kill -9`), so a hard-killed orchestrator never wedges the next launch — no
staleness heuristics, no refuse-forever state.

The lock path is derived ENTIRELY from `--workflow`, the process's only project
identity, keyed on the workflow's PARENT DIRECTORY: `WORKFLOW.md` and
`WORKFLOW.pilot-codex.md` in one project resolve to the SAME lock, so the pilot
and production self-orchestrators are mutually exclusive. Keying on the filename
would hand them separate locks and reproduce the incident.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

LOCK_DIRNAME = ".run"  # gitignored at any depth (.gitignore: `.run/`)
LOCK_FILENAME = "orchestrator.lock"
UNKNOWN_HOLDER = "unknown"


class SingletonLockError(RuntimeError):
    """Another orchestrator process already holds this project's lock."""


def lock_path_for(workflow_path: Path) -> Path:
    """The lock file for the project owning `workflow_path` (parent-keyed)."""
    return Path(workflow_path).resolve().parent / LOCK_DIRNAME / LOCK_FILENAME


def acquire_singleton_lock(workflow_path: Path) -> int:
    """Take this project's singleton lock; return the fd, which MUST be kept.

    The returned fd is the lock: `fcntl.flock` rides the open file
    *description*, so letting it close releases the lock. A raw `os.open` int is
    never closed by refcounting (unlike a builtin `open()` object falling out of
    scope) — the caller stores it for the process lifetime.

    Raises `SingletonLockError` naming the lock path and the holder's pid when
    another process holds it.
    """
    path = lock_path_for(workflow_path)
    path.parent.mkdir(parents=True, exist_ok=True)  # .run/ is gitignored: absent
                                                    # in a fresh clone, and in
                                                    # every test tmp_path
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _read_holder_pid(path)
        os.close(fd)
        raise SingletonLockError(
            f"another orchestrator already holds {path} (holder pid {holder}); "
            f"refusing to start a second orchestrator for this project. That pid "
            f"is the python child, not the `uv run` wrapper a process tree shows. "
            f"Verify before acting on it (`ps -p <pid> -o command=`): the holder "
            f"publishes its pid AFTER taking the lock, so a refuser racing that "
            f"write can read a previous holder's pid (codex review, PR #139)"
        ) from None
    except BaseException:
        os.close(fd)
        raise

    # PID protocol: BSD flock has no query interface, and `fcntl.lockf` is a
    # different, non-interacting lock family that must not be mixed in — so the
    # holder publishes its pid in the locked file for the refuser to read.
    # `os.write` on a raw fd is unbuffered: the bytes are visible to a reader
    # the moment it returns.
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    return fd


def _read_holder_pid(path: Path) -> str:
    """The holder's pid as text, `<pid> (stale: process dead)`, or `unknown`.

    An empty file is the normal race window (the holder took the flock but has
    not written its pid yet), not an error. A published pid can also be STALE:
    the new holder overwrites the file only after `flock` succeeds, so a
    refuser racing that write reads the PREVIOUS holder's pid (codex review,
    PR #139). A dead pid is labelled stale rather than reported as the holder
    — pid reuse would otherwise send the operator to an unrelated process. A
    live-but-reused pid cannot be distinguished here; the error text tells the
    operator to verify before acting.
    """
    try:
        raw = path.read_text().strip()
    except OSError:
        return UNKNOWN_HOLDER
    if not raw.isdigit():
        return UNKNOWN_HOLDER
    try:
        os.kill(int(raw), 0)
    except ProcessLookupError:
        return f"{raw} (stale: process dead)"
    except OSError:
        pass  # EPERM etc.: process exists but is not ours to signal
    return raw
