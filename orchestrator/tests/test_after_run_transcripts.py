"""Ground-truth transcript capture in hooks/after_run.sh (issue #30).

The after_run hook copies the CLI session transcript(s) for the per-issue
workspace into `.run/transcripts/` so a fresh fail-review verifier (#20b) and
#16's mechanical fallback read them from disk (ADR-013 inversion) instead of
trusting returned summaries. Transcripts carry secrets — `.run/` is gitignored
and their content NEVER reaches GitHub.

These tests drive the real hook file as a subprocess, mirroring how the
orchestrator runs it (`_run_hook`: cwd == workspace, env inherits os.environ).

implements: #30 (fail-review evidence tier)
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK = REPO_ROOT / "hooks" / "after_run.sh"


def _encode(cwd: Path) -> str:
    """Claude Code's project-dir name: the absolute cwd with every "/" and "."
    replaced by "-" (matches the hook's `sed 's#[/.]#-#g'`)."""
    return re.sub(r"[/.]", "-", str(cwd))


def _make_source(claude_config_dir: Path, workspace: Path) -> Path:
    """Create the fake CLI transcript source dir for `workspace`, returning it."""
    src = claude_config_dir / "projects" / _encode(workspace)
    src.mkdir(parents=True, exist_ok=True)
    return src


def _run_hook(workspace: Path, claude_config_dir: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    # Pin PWD so the hook's `$PWD`-based encoding is deterministic regardless of
    # symlinks in the tmp path; this is exactly what the orchestrator's cwd is.
    env["PWD"] = str(workspace)
    return subprocess.run(
        ["bash", str(HOOK)],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture()
def claude_config_dir(tmp_path: Path) -> Path:
    cfg = tmp_path / "claude"
    cfg.mkdir()
    return cfg


def test_copies_completed_transcripts_into_run_dir(
    workspace: Path, claude_config_dir: Path
) -> None:
    src = _make_source(claude_config_dir, workspace)
    (src / "session-a.jsonl").write_text('{"role":"user"}\n')
    (src / "session-b.jsonl").write_text('{"role":"assistant"}\n')

    result = _run_hook(workspace, claude_config_dir)
    assert result.returncode == 0, result.stderr

    dest = workspace / ".run" / "transcripts"
    assert (dest / "session-a.jsonl").read_text() == '{"role":"user"}\n'
    assert (dest / "session-b.jsonl").read_text() == '{"role":"assistant"}\n'


def test_no_op_when_source_dir_absent(workspace: Path, claude_config_dir: Path) -> None:
    # projects/<encoded> was never created — the hook must not error or create
    # the destination.
    result = _run_hook(workspace, claude_config_dir)
    assert result.returncode == 0, result.stderr
    assert not (workspace / ".run" / "transcripts").exists()


def test_idempotent_overwrites_stale_copies(
    workspace: Path, claude_config_dir: Path
) -> None:
    src = _make_source(claude_config_dir, workspace)
    transcript = src / "session.jsonl"

    transcript.write_text("v1\n")
    assert _run_hook(workspace, claude_config_dir).returncode == 0
    dest = workspace / ".run" / "transcripts" / "session.jsonl"
    assert dest.read_text() == "v1\n"

    # Reused workspace, transcript grew: a second run refreshes the stale copy.
    transcript.write_text("v1\nv2\n")
    assert _run_hook(workspace, claude_config_dir).returncode == 0
    assert dest.read_text() == "v1\nv2\n"


def test_transcript_content_never_reaches_git(
    workspace: Path, claude_config_dir: Path
) -> None:
    # A workspace is a git clone; the real repo .gitignore must ignore `.run/`.
    subprocess.run(["git", "init", "-q"], cwd=str(workspace), check=True)
    (workspace / ".gitignore").write_text((REPO_ROOT / ".gitignore").read_text())

    src = _make_source(claude_config_dir, workspace)
    (src / "session.jsonl").write_text('{"secret":"token"}\n')

    assert _run_hook(workspace, claude_config_dir).returncode == 0

    # The copied transcript exists on disk but is invisible to git: nothing under
    # .run is stageable, so it can never be committed or pushed to GitHub.
    assert (workspace / ".run" / "transcripts" / "session.jsonl").exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert ".run" not in status
    assert "transcript" not in status
    ignored = subprocess.run(
        ["git", "check-ignore", ".run/transcripts/session.jsonl"],
        cwd=str(workspace),
        capture_output=True,
        text=True,
    )
    assert ignored.returncode == 0, "transcript path must be gitignored"


def test_repo_gitignore_lists_run_dir() -> None:
    # Guards the .gitignore edit: `.run/` must stay ignored repo-wide.
    assert ".run/" in (REPO_ROOT / ".gitignore").read_text().splitlines()


def test_transcripts_unstageable_in_repos_that_do_not_ignore_run_dir(
    workspace: Path, claude_config_dir: Path
) -> None:
    # Codex PR #36 P1: registered projects' repos generally do NOT ignore
    # `.run/` — only Switchboard's own .gitignore was updated. The hook must
    # make the copy invisible to git in ANY clone (repo-local exclude), or an
    # agent's `git add -A` could stage and push secret-bearing transcripts.
    subprocess.run(["git", "init", "-q"], cwd=str(workspace), check=True)
    # No .gitignore at all — the worst-case target repo.

    src = _make_source(claude_config_dir, workspace)
    (src / "session.jsonl").write_text('{"secret":"token"}\n')

    assert _run_hook(workspace, claude_config_dir).returncode == 0

    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(workspace),
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".run" not in status

    # Even a blanket add stages nothing from .run/.
    subprocess.run(["git", "add", "-A"], cwd=str(workspace), check=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], cwd=str(workspace),
        capture_output=True, text=True, check=True,
    ).stdout
    assert ".run" not in staged

    # And the hook is idempotent about the exclude entry on reused workspaces.
    assert _run_hook(workspace, claude_config_dir).returncode == 0
    exclude = (workspace / ".git" / "info" / "exclude").read_text()
    assert exclude.count(".run/") == 1
