"""Regression coverage for the reviewed Stage 7 circuit-canary procedure."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from orchestrator.workflow import Config, load_workflow, validate_dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECT = REPO_ROOT / "projects" / "mixed-canary"
WORKFLOW = PROJECT / "WORKFLOW.circuit-recovery.md"
BASELINE_WORKFLOW = PROJECT / "WORKFLOW.md"
ROLLBACK_WORKFLOW = PROJECT / "WORKFLOW.rollback-claude.md"
LAUNCHER = REPO_ROOT / "scripts" / "run-stage7-circuit-canary.sh"
INJECTOR = REPO_ROOT / "scripts" / "codex-circuit-canary.sh"


@pytest.mark.parametrize(
    ("phase", "cli", "labels", "dispatch", "workflow"),
    [
        (
            "circuit-recovery",
            "mixed",
            "status:todo,gate:triage-passed,agent:codex",
            "codex",
            "WORKFLOW.circuit-recovery.md",
        ),
        (
            "rollback-claude",
            "default (flag omitted)",
            "status:todo,gate:triage-passed,provider:codex",
            "claude",
            "WORKFLOW.rollback-claude.md",
        ),
    ],
)
def test_stage7_checkpoint_dry_run_is_offline_and_exact(
    phase: str,
    cli: str,
    labels: str,
    dispatch: str,
    workflow: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / "gh-was-called"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8"
    )
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(LAUNCHER), phase, "--dry-run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "dry-run invoked gh"
    assert f"cli provider: {cli}" in result.stdout
    assert f"issue labels: {labels}" in result.stdout
    assert f"expected dispatch provider: {dispatch}" in result.stdout
    workflow_line = next(
        line for line in result.stdout.splitlines() if line.startswith("workflow: ")
    )
    assert workflow_line.endswith(f"/projects/mixed-canary/{workflow}")
    assert "no GitHub writes and no process launch" in result.stdout


def test_stage7_launcher_rejects_combined_or_unknown_run() -> None:
    result = subprocess.run(
        ["bash", str(LAUNCHER), "all-at-once"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "circuit-recovery|rollback-claude" in result.stderr


def test_circuit_workflow_is_isolated_and_capacity_one(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    definition = load_workflow(WORKFLOW)
    cfg = Config(definition, PROJECT)
    baseline = Config(load_workflow(BASELINE_WORKFLOW), PROJECT)

    validate_dispatch(cfg, provider_id="mixed")
    mixed = cfg.mixed()

    assert cfg.tracker().repo == "colin-prologue/switchboard-mixed-canary"
    assert cfg.agent().max_concurrent_agents == 1
    assert mixed.max_concurrent_agents_by_provider == {"claude": 1, "codex": 1}
    assert mixed.weights == baseline.mixed().weights == {"claude": 100, "codex": 0}
    assert mixed.codex.command == str(INJECTOR)
    assert "recovery probe" in definition.prompt_template
    assert "Do not merge the pull request" in definition.prompt_template


def test_circuit_injector_fails_once_then_delegates_with_unchanged_io(
    tmp_path: Path,
) -> None:
    first = subprocess.run(
        [str(INJECTOR), "exec", "--json", "-"],
        cwd=tmp_path,
        input="first prompt",
        capture_output=True,
        text=True,
        check=False,
    )

    assert first.returncode == 1
    records = [json.loads(line) for line in first.stdout.splitlines()]
    assert records[-1] == {
        "type": "error",
        "error": {
            "code": "service_unavailable",
            "message": "deterministic mixed-canary provider outage",
        },
    }
    assert (tmp_path / ".run" / "stage7-circuit-failure-injected").is_file()

    argv_file = tmp_path / "argv"
    stdin_file = tmp_path / "stdin"
    fake_codex = tmp_path / "real-codex"
    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >\"$ARGV_FILE\"\n"
        "cat >\"$STDIN_FILE\"\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    env = {
        **os.environ,
        "SWITCHBOARD_CANARY_CODEX_BIN": str(fake_codex),
        "ARGV_FILE": str(argv_file),
        "STDIN_FILE": str(stdin_file),
    }

    second = subprocess.run(
        [str(INJECTOR), "exec", "--ignore-user-config", "--json", "-"],
        cwd=tmp_path,
        input="recovery prompt",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert argv_file.read_text(encoding="utf-8").strip() == (
        "exec --ignore-user-config --json -"
    )
    assert stdin_file.read_text(encoding="utf-8") == "recovery prompt"


def test_stage7_procedure_pins_evidence_and_unchanged_rollback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    source = LAUNCHER.read_text(encoding="utf-8")

    assert os.access(LAUNCHER, os.X_OK)
    assert os.access(INJECTOR, os.X_OK)
    assert "cooldown_ms=300000" in source
    assert "retry_disposition=provider_wait" in source
    assert 'HALF_OPEN_COUNT' in source
    assert 'DISPATCH_COUNT' in source
    assert 'SESSION_ONE_COUNT' in source
    assert 'WORKFLOW.rollback-claude.md' in source
    assert 'RUN_MODE="default-claude"' in source
    assert "unset SWITCHBOARD_CANARY_CODEX_BIN" in source
    assert "SECONDS + 2700" in source
    assert "OPEN_ISSUES" in source and "OPEN_PRS" in source

    rollback_definition = load_workflow(ROLLBACK_WORKFLOW)
    rollback = Config(rollback_definition, PROJECT)
    validate_dispatch(rollback)
    assert set(rollback_definition.config["providers"]) == {"claude"}
    assert "routing" not in rollback_definition.config


def test_stage7_issue_contracts_are_sequential_and_executable() -> None:
    bodies = sorted((PROJECT / "stage7-checkpoints").glob("*.md"))

    assert [path.name for path in bodies] == [
        "01-circuit-recovery.md",
        "02-rollback-claude.md",
    ]
    for body in bodies:
        text = body.read_text(encoding="utf-8")
        assert "## Acceptance criteria" in text
        assert "python3 -m unittest discover -s tests -v" in text
        assert "body closes this issue when merged" in text
        assert "Do not merge it" in text
