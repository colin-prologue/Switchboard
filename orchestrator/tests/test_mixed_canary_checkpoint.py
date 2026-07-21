"""Regression coverage for the reviewed Stage 6 native checkpoint procedure."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.workflow import Config, load_workflow, validate_dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run-mixed-canary-checkpoint.sh"
PROJECT = REPO_ROOT / "projects" / "mixed-canary"
ROLLBACK_WORKFLOW = PROJECT / "WORKFLOW.rollback-claude.md"

PHASES = {
    "explicit-claude": {
        "cli": "mixed",
        "labels": "status:todo,gate:triage-passed,agent:claude",
        "dispatch": "claude",
        "durable": "claude",
    },
    "explicit-codex": {
        "cli": "mixed",
        "labels": "status:todo,gate:triage-passed,agent:codex",
        "dispatch": "codex",
        "durable": "codex",
    },
    "weighted-claude": {
        "cli": "mixed",
        "labels": "status:todo,gate:triage-passed",
        "dispatch": "claude",
        "durable": "claude",
    },
    "rollback-claude": {
        "cli": "default (flag omitted)",
        "labels": "status:todo,gate:triage-passed,provider:codex",
        "dispatch": "claude",
        "durable": "codex",
    },
}


@pytest.mark.parametrize(("phase", "expected"), PHASES.items())
def test_checkpoint_dry_run_is_offline_and_declares_exact_contract(
    phase: str,
    expected: dict[str, str],
    tmp_path: Path,
) -> None:
    marker = tmp_path / "gh-was-called"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(SCRIPT), phase, "--dry-run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "checkpoint dry-run invoked gh"
    assert f"cli provider: {expected['cli']}" in result.stdout
    assert f"issue labels: {expected['labels']}" in result.stdout
    assert f"expected dispatch provider: {expected['dispatch']}" in result.stdout
    assert f"expected durable provider label: {expected['durable']}" in result.stdout
    assert "no GitHub writes and no process launch" in result.stdout


def test_checkpoint_rejects_unknown_phase_without_side_effects() -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "all-at-once"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "explicit-claude|explicit-codex|weighted-claude|rollback-claude" in (
        result.stderr
    )


def test_checkpoint_script_is_executable_and_pins_named_stop_conditions() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert os.access(SCRIPT, os.X_OK)
    assert 'OPEN_ISSUES="$(gh_clean issue list' in source
    assert 'OPEN_PRS="$(gh_clean pr list' in source
    assert "trap cleanup EXIT" in source
    assert "SECONDS + 1800" in source
    for status in ("human-review", "parked", "blocked", "drafting", "plan-review"):
        assert f"status:{status}" in source


def test_rollback_workflow_is_strict_claude_only(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    definition = load_workflow(ROLLBACK_WORKFLOW)
    cfg = Config(definition, ROLLBACK_WORKFLOW.parent)

    validate_dispatch(cfg)

    assert set(definition.config["providers"]) == {"claude"}
    assert "routing" not in definition.config
    assert cfg.tracker().repo == "colin-prologue/switchboard-mixed-canary"
    assert cfg.agent().max_concurrent_agents == 1
    assert cfg.claude().command.startswith("claude -p --verbose")
    assert "existing `provider:codex` label is deliberate" in (
        definition.prompt_template
    )


def test_checkpoint_issue_contracts_are_sequential_and_executable() -> None:
    bodies = sorted((PROJECT / "checkpoints").glob("*.md"))

    assert [path.name for path in bodies] == [
        "01-explicit-claude.md",
        "02-explicit-codex.md",
        "03-weighted-claude.md",
        "04-rollback-claude.md",
    ]
    for body in bodies:
        text = body.read_text(encoding="utf-8")
        assert "## Acceptance criteria" in text
        assert "python3 -m unittest discover -s tests -v" in text
        assert "body closes this issue when merged" in text
        assert "Do not merge it" in text
