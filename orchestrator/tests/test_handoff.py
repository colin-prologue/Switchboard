"""issue #61: terminal-safe handoff evidence contract tests.

Validation matrix over real git workspaces (tmp repos, not path fakes) and a
fake tracker whose PR surface mirrors `fetch_open_prs`' contract.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from orchestrator.handoff import (
    EVIDENCE_RELPATH,
    HandoffEvidence,
    HandoffRejection,
    read_evidence,
    validate_handoff,
)


class FakePRTracker:
    def __init__(self, prs: list[dict] | None = None):
        self.prs = prs or []
        self.queried_refs: list[str] = []

    async def fetch_open_prs(self, head_ref: str) -> list[dict]:
        self.queried_refs.append(head_ref)
        return list(self.prs)


def _git(ws: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(ws), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q", "-b", "switchboard/issue-7")
    _git(ws, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "seed")
    (ws / ".run").mkdir()
    return ws


def _write_evidence(ws: Path, **overrides) -> dict:
    data = {
        "issue": "7",
        "pr_number": 12,
        "head_sha": _git(ws, "rev-parse", "HEAD"),
        **overrides,
    }
    (ws / EVIDENCE_RELPATH).write_text(json.dumps(data), encoding="utf-8")
    return data


def _pr_for(ws: Path, **overrides) -> dict:
    return {
        "number": 12,
        "head_sha": _git(ws, "rev-parse", "HEAD"),
        "closes": [7],
        **overrides,
    }


def test_no_evidence_file_is_not_a_handoff_attempt(workspace: Path) -> None:
    assert read_evidence(workspace) is None


@pytest.mark.parametrize(
    "content",
    [
        "not json {",
        '["a", "list"]',
        '{"pr_number": 12, "head_sha": "x"}',            # missing issue
        '{"issue": "7", "head_sha": "x"}',               # missing pr_number
        '{"issue": "7", "pr_number": 12}',               # missing head_sha
        '{"issue": "7", "pr_number": true, "head_sha": "x"}',
        '{"issue": "", "pr_number": 12, "head_sha": "x"}',
        '{"issue": "7", "pr_number": -1, "head_sha": "x"}',
    ],
)
def test_malformed_evidence_is_rejected(workspace: Path, content: str) -> None:
    (workspace / EVIDENCE_RELPATH).write_text(content, encoding="utf-8")
    parsed = read_evidence(workspace)
    assert isinstance(parsed, HandoffRejection)
    assert parsed.reason == "malformed_evidence"


@pytest.mark.asyncio
async def test_valid_evidence_passes_full_validation(workspace: Path) -> None:
    _write_evidence(workspace)
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert rejection is None
    assert isinstance(evidence, HandoffEvidence)
    assert evidence.pr_number == 12
    assert tracker.queried_refs == ["switchboard/issue-7"]


@pytest.mark.asyncio
async def test_wrong_issue_linkage_in_evidence_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace, issue="8")
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "issue_mismatch"
    assert tracker.queried_refs == []  # rejected before any tracker call


@pytest.mark.asyncio
async def test_stale_evidence_head_mismatch_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace)
    _git(workspace, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "--allow-empty", "-m", "moved on")
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "head_mismatch"


@pytest.mark.asyncio
async def test_missing_pr_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace)
    evidence, rejection = await validate_handoff(FakePRTracker([]), workspace, "7")
    assert evidence is None and rejection.reason == "missing_pr"


@pytest.mark.asyncio
async def test_multiple_open_prs_are_rejected(workspace: Path) -> None:
    _write_evidence(workspace)
    tracker = FakePRTracker([_pr_for(workspace), _pr_for(workspace, number=13)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "multiple_prs"


@pytest.mark.asyncio
async def test_pr_number_mismatch_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace, pr_number=99)
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "pr_mismatch"


@pytest.mark.asyncio
async def test_unpushed_head_pr_head_mismatch_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace)
    tracker = FakePRTracker([_pr_for(workspace, head_sha="0" * 40)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "pr_head_mismatch"


@pytest.mark.asyncio
async def test_pr_without_closes_linkage_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace)
    tracker = FakePRTracker([_pr_for(workspace, closes=[99])])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "pr_linkage_missing"


@pytest.mark.asyncio
async def test_non_git_workspace_is_rejected_not_crashed(tmp_path: Path) -> None:
    ws = tmp_path / "not-a-repo"
    (ws / ".run").mkdir(parents=True)
    (ws / EVIDENCE_RELPATH).write_text(
        '{"issue": "7", "pr_number": 12, "head_sha": "abc"}', encoding="utf-8"
    )
    evidence, rejection = await validate_handoff(FakePRTracker(), ws, "7")
    assert evidence is None and rejection.reason == "workspace_unreadable"


@pytest.mark.asyncio
async def test_stale_evidence_from_prior_turn_is_rejected(workspace: Path) -> None:
    # PR #115 review: evidence surviving a failed turn must not validate on a
    # later successful turn unless rewritten during that turn.
    from orchestrator.handoff import snapshot_evidence

    _write_evidence(workspace)
    prior = snapshot_evidence(workspace)
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(
        tracker, workspace, "7", prior_snapshot=prior)
    assert evidence is None and rejection.reason == "evidence_not_fresh"

    # Rewriting the file (even with identical fields) during the turn is fresh.
    _write_evidence(workspace)
    evidence, rejection = await validate_handoff(
        tracker, workspace, "7", prior_snapshot=prior)
    assert rejection is None and evidence is not None


@pytest.mark.asyncio
async def test_dirty_worktree_outside_run_dir_is_rejected(workspace: Path) -> None:
    _write_evidence(workspace)
    (workspace / "forgotten.py").write_text("x = 1\n", encoding="utf-8")
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert evidence is None and rejection.reason == "workspace_dirty"
    assert "forgotten.py" in rejection.detail


@pytest.mark.asyncio
async def test_run_dir_contents_do_not_count_as_dirty(workspace: Path) -> None:
    # .run/ is the orchestrator's own channel (evidence, transcripts) and is
    # excluded from the clean-worktree guard.
    _write_evidence(workspace)
    (workspace / ".run" / "transcript.jsonl").write_text("{}", encoding="utf-8")
    tracker = FakePRTracker([_pr_for(workspace)])
    evidence, rejection = await validate_handoff(tracker, workspace, "7")
    assert rejection is None and evidence is not None
