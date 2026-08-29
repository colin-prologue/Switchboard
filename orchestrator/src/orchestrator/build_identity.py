"""Which build this process loaded (issue #143).

The 2026-08-09 incident: a long-running orchestrator kept applying a pre-#35
session-cap rule for days after the fix had merged, and nothing in the log said
which build it was running — a healthy start read identically whether the code
was an hour or a week old, so diagnosis meant noticing a park-message string no
longer existed at HEAD and bisecting for when it changed. #140/#149 built the
staleness *detector*; the detector only speaks when something is already wrong.
This is the identity *statement* that makes a healthy start self-describing.

Resolution order for `sha`:

1. `SB_LAUNCH_SHA` when bound and non-empty. `run-project.sh` captures it
   before the process starts (`scripts/run-project.sh:29-30`), so it is correct
   by construction even if the tree moves between launch and now.
2. `git rev-parse HEAD` against the MODULE'S OWN source tree — never the
   process cwd. launchd starts from an arbitrary directory, so the cwd answers
   a different question than "which build is this process running".
3. `"unknown"`. No `.git`, no `git` on PATH, an exported copy. Startup must
   never fail on this: an orchestrator that refuses to run because it cannot
   describe itself is strictly worse than one that says so and carries on.

`dirty` is always resolved from the module tree — the env var carries no
worktree state — so a launch with `SB_LAUNCH_SHA` bound and an unreadable tree
reports a known sha alongside `dirty=unknown`.

Both fields are strings, `dirty` included. `dirty=unknown` has to share a
column with `dirty=true`, and one type per field is what keeps the record
greppable.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

UNKNOWN = "unknown"

# The directory the orchestrator package was imported from. `git -C` walks up
# from here, so a source checkout resolves its repo root and a copy installed
# outside any work tree resolves nothing (-> UNKNOWN).
MODULE_TREE = Path(__file__).resolve().parent

# A wedged git must not hold startup open. Bounded for the same reason the
# freshness preflight is bounded — except that this one blocks, deliberately:
# it runs once, before the poll loop has any work to interleave with, and a
# synchronous call keeps the identity resolved before the record is emitted
# rather than a tick later.
GIT_TIMEOUT_S = 5.0


def resolve_build_identity(
    env: Mapping[str, str] | None = None,
    tree: Path | None = None,
) -> tuple[str, str]:
    """Return `(sha, dirty)` for the build this process loaded. Never raises."""
    env = os.environ if env is None else env
    tree = MODULE_TREE if tree is None else tree

    # Falsiness, not membership (the freshness skip rule's precedent):
    # `SB_LAUNCH_SHA=""` is what a `set -a` over a blank assignment yields, and
    # an empty sha identifies nothing.
    sha = env.get("SB_LAUNCH_SHA") or _head(tree) or UNKNOWN
    return sha, _dirty(tree)


def _head(tree: Path) -> str | None:
    out = _git(tree, "rev-parse", "HEAD")
    return out.strip() if out is not None else None


def _dirty(tree: Path) -> str:
    # `status --porcelain` needs the raw stdout, not a stripped-or-None
    # helper: empty output is CLEAN and the two must not collapse, or an
    # unreadable index would report a pristine tree (the `handoff.py`
    # `_git_status_porcelain` lesson).
    out = _git(tree, "status", "--porcelain")
    if out is None:
        return UNKNOWN
    return "true" if out.strip() else "false"


def _git(tree: Path, *args: str) -> str | None:
    """git's stdout, or None when git could not answer — no binary on PATH, no
    work tree, a non-zero exit, or a timeout. Every one of those is a legible
    `"unknown"`, never a failed startup."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(tree), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")
