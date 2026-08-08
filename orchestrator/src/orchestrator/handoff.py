"""Terminal-safe worker handoff evidence contract (issue #61).

Workers never mutate `status:*` labels. Instead, a worker's FINAL task action
is writing `.run/handoff-evidence.json` in its workspace:

    {"issue": "<identifier>", "pr_number": <int>, "head_sha": "<sha>"}

The orchestrator — and only the orchestrator — performs the handoff label
transition, and only after BOTH hold:

1. the provider turn returned terminal success (`TurnResult.status ==
   "succeeded"`), and
2. the evidence validates against observed state: the file parses, names this
   issue, its `head_sha` matches the workspace's actual HEAD, and exactly one
   open PR exists for the workspace branch whose head oid matches the same
   sha (proving the commit was pushed and the PR is current).

Evidence written mid-turn is inert until the provider result arrives — that
ordering is what closes the Stage 7 race where handoff evidence exists while
the provider process is still active or later fails. Any rejection is a
diagnostic, never a transition: the issue keeps its current status and the
workspace is preserved.

`.run/` is workspace-local and git-excluded (same convention as raw
transcripts), so evidence never lands in PRs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

EVIDENCE_RELPATH = Path(".run") / "handoff-evidence.json"


def snapshot_evidence(workspace: Path) -> tuple[int, str] | None:
    """Pre-turn snapshot of the evidence file: (mtime_ns, sha256) or None when
    absent. The scheduler captures this before EVERY provider turn; validation
    then requires the file to have been created or changed during the turn
    that succeeded — evidence left over from an earlier failed turn is stale
    and must not transition the issue (PR #115 review finding)."""
    path = workspace / EVIDENCE_RELPATH
    try:
        raw = path.read_bytes()
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    return (mtime, hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class HandoffEvidence:
    issue: str
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class HandoffRejection:
    """Why evidence did not validate. A rejection NEVER causes a transition."""

    reason: str
    detail: str


async def _git_output(workspace: Path, *args: str) -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(workspace), *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", "replace").strip() or None


async def _git_status_porcelain(workspace: Path) -> str | None:
    """`git status --porcelain`, distinguishing FAILURE (None) from CLEAN ("").

    `_git_output` collapses both into None (empty stdout is its miss signal),
    which would let a corrupt/unreadable index pass as a clean worktree
    (PR #115 review round 3).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(workspace), "status", "--porcelain",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", "replace")


def read_evidence(workspace: Path) -> HandoffEvidence | HandoffRejection | None:
    """Parse the evidence file. None = no file (worker not handing off)."""
    path = workspace / EVIDENCE_RELPATH
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        return HandoffRejection("evidence_unreadable", str(exc))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return HandoffRejection("malformed_evidence", f"invalid json: {exc}")
    if not isinstance(data, dict):
        return HandoffRejection("malformed_evidence", "evidence is not an object")

    issue = data.get("issue")
    pr_number = data.get("pr_number")
    head_sha = data.get("head_sha")
    if not isinstance(issue, str) or not issue.strip():
        return HandoffRejection("malformed_evidence", "missing/invalid 'issue'")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        return HandoffRejection("malformed_evidence", "missing/invalid 'pr_number'")
    if not isinstance(head_sha, str) or not head_sha.strip():
        return HandoffRejection("malformed_evidence", "missing/invalid 'head_sha'")
    return HandoffEvidence(
        issue=issue.strip(), pr_number=pr_number, head_sha=head_sha.strip()
    )


async def validate_handoff(
    tracker,
    workspace: Path,
    issue_identifier: str,
    prior_snapshot: tuple[int, str] | None = None,
) -> tuple[HandoffEvidence | None, HandoffRejection | None]:
    """Full validation: (evidence, None) on success; (None, rejection) on any
    defect; (None, None) when no evidence file exists (not a handoff attempt).

    The tracker needs one method beyond the issue surface:
    `fetch_open_prs(head_ref)` returning `[{"number": int, "head_sha": str}]`.
    """
    parsed = read_evidence(workspace)
    if parsed is None:
        return None, None
    if isinstance(parsed, HandoffRejection):
        return None, parsed

    # Freshness: the evidence must have been created or changed during the
    # turn that just succeeded. `prior_snapshot` is the pre-turn state; an
    # identical post-turn snapshot means this is leftover evidence from an
    # earlier (failed/incomplete) turn — the Stage 7 race through the side
    # door of a later unrelated success (PR #115 review).
    if prior_snapshot is not None and snapshot_evidence(workspace) == prior_snapshot:
        return None, HandoffRejection(
            "evidence_not_fresh",
            "evidence file unchanged since before this provider turn; a "
            "handing-off worker must (re)write it in the turn that succeeds",
        )

    if parsed.issue != issue_identifier:
        return None, HandoffRejection(
            "issue_mismatch",
            f"evidence names issue {parsed.issue!r}, session owns {issue_identifier!r}",
        )

    head = await _git_output(workspace, "rev-parse", "HEAD")
    if head is None:
        return None, HandoffRejection("workspace_unreadable", "git rev-parse HEAD failed")
    if head != parsed.head_sha:
        return None, HandoffRejection(
            "head_mismatch",
            f"evidence head {parsed.head_sha} != workspace HEAD {head} (stale evidence)",
        )

    # Clean-worktree guard (parity with the retired canary hook): tracked
    # modifications or untracked task files mean the PR omits workspace work —
    # an incomplete implementation must not reach review. `.run/` is the
    # orchestrator's own workspace-local channel (evidence, transcripts) and
    # is excluded from the check.
    porcelain = await _git_status_porcelain(workspace)
    if porcelain is None:
        return None, HandoffRejection(
            "workspace_unreadable", "git status --porcelain failed"
        )
    dirty = [
        line for line in porcelain.splitlines()
        if line.strip() and not line[3:].startswith(".run/")
    ]
    if dirty:
        return None, HandoffRejection(
            "workspace_dirty",
            f"{len(dirty)} uncommitted path(s) outside .run/ "
            f"(first: {dirty[0].strip()!r}); the PR omits workspace changes",
        )

    branch = await _git_output(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None or branch == "HEAD":
        return None, HandoffRejection(
            "workspace_unreadable", f"cannot resolve workspace branch (got {branch!r})"
        )

    prs = await tracker.fetch_open_prs(branch)
    if not prs:
        return None, HandoffRejection(
            "missing_pr", f"no open PR for branch {branch!r}"
        )
    if len(prs) > 1:
        return None, HandoffRejection(
            "multiple_prs",
            f"{len(prs)} open PRs for branch {branch!r}; exactly one required",
        )
    pr = prs[0]
    if pr["number"] != parsed.pr_number:
        return None, HandoffRejection(
            "pr_mismatch",
            f"evidence names PR #{parsed.pr_number}, open PR is #{pr['number']}",
        )
    if pr["head_sha"] != parsed.head_sha:
        return None, HandoffRejection(
            "pr_head_mismatch",
            f"PR #{pr['number']} head {pr['head_sha']} != evidence head "
            f"{parsed.head_sha} (unpushed or diverged)",
        )
    try:
        issue_number = int(issue_identifier)
    except ValueError:
        issue_number = None
    closes = pr.get("closes") or []
    if issue_number is not None and issue_number not in closes:
        return None, HandoffRejection(
            "pr_linkage_missing",
            f"PR #{pr['number']} does not close issue #{issue_number} "
            f"(closing refs: {closes}); merge would strand the issue open",
        )
    return parsed, None
