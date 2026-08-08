"""Regression coverage for the reviewed Stage 7 circuit-canary procedure."""

from __future__ import annotations

import json
import os
import subprocess

from orchestrator.failure_classification import classify_codex_failure
from orchestrator.types import FailureClass
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
HANDOFF_HOOK = REPO_ROOT / "scripts" / "stage7-circuit-after-run.sh"
NATIVE_INJECTOR_COMMAND = (
    "/Users/colindwan/Developer/Switchboard/scripts/codex-circuit-canary.sh "
    "--ask-for-approval never --sandbox workspace-write "
    "--config sandbox_workspace_write.network_access=true"
)


@pytest.mark.parametrize(
    ("phase", "title", "cli", "labels", "dispatch", "workflow"),
    [
        (
            "circuit-recovery",
            "Stage 7 circuit checkpoint: terminal Codex recovery",
            "mixed",
            "status:todo,gate:triage-passed,agent:codex",
            "codex",
            "WORKFLOW.circuit-recovery.md",
        ),
        (
            "rollback-claude",
            "Stage 7 circuit checkpoint: Claude-only rollback",
            "default (flag omitted)",
            "status:todo,gate:triage-passed,provider:codex",
            "claude",
            "WORKFLOW.rollback-claude.md",
        ),
    ],
)
def test_stage7_checkpoint_dry_run_is_offline_and_exact(
    phase: str,
    title: str,
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
    assert f"title: {title}" in result.stdout
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
    assert cfg.agent().max_turns == 1
    assert mixed.max_concurrent_agents_by_provider == {"claude": 1, "codex": 1}
    assert mixed.weights == baseline.mixed().weights == {"claude": 100, "codex": 0}
    assert mixed.codex.command == NATIVE_INJECTOR_COMMAND
    assert "stage7-circuit-after-run.sh" in cfg.hooks().after_run
    assert "recovery probe" in definition.prompt_template
    # issue #61: the canary agent writes the PRODUCTION evidence contract;
    # the legacy ready-marker is gone.
    assert ".run/handoff-evidence.json" in definition.prompt_template
    assert "stage7-handoff-ready" not in definition.prompt_template
    assert "without changing" in definition.prompt_template
    assert "issue labels" in definition.prompt_template
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
    # issue #109: injection must be REAL-shaped — turn.failed nesting
    # error.message with NO code (ground truth: fixtures/codex_cli_auth_401
    # .jsonl), so the circuit path exercised is the _TEXT_PATTERNS path real
    # Codex traffic takes.
    terminal = records[-1]
    assert terminal["type"] == "turn.failed"
    assert "code" not in terminal["error"]
    # Must classify to a COOLDOWN class (PROVIDER_UNAVAILABLE), not a latched
    # one — the recovery canary needs open_cooldown -> half-open -> recovery
    # (run-stage7-circuit-canary.sh greps for exactly that transition).
    assert (
        classify_codex_failure(
            code=None, detail=terminal["error"]["message"]
        )
        is FailureClass.PROVIDER_UNAVAILABLE
    )
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
        [
            str(INJECTOR),
            "--ask-for-approval",
            "never",
            "--sandbox",
            "workspace-write",
            "--config",
            "sandbox_workspace_write.network_access=true",
            "exec",
            "--ignore-user-config",
            "--json",
            "-",
        ],
        cwd=tmp_path,
        input="recovery prompt",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert argv_file.read_text(encoding="utf-8").strip() == (
        "--ask-for-approval never --sandbox workspace-write "
        "--config sandbox_workspace_write.network_access=true "
        "exec --ignore-user-config --json -"
    )
    assert stdin_file.read_text(encoding="utf-8") == "recovery prompt"


def test_stage7_handoff_hook_is_a_base_passthrough_with_repo_guard(
    tmp_path: Path,
) -> None:
    """issue #61: the canary hook no longer validates or moves labels — that
    ownership is production (orchestrator handoff validation + single swap).
    The hook must call the base after_run, enforce the canary repo guard, and
    perform no gh mutations regardless of evidence presence."""
    workspace = tmp_path / "ws"
    (workspace / ".run").mkdir(parents=True)
    base_called = tmp_path / "base-called"
    base_hook = tmp_path / "base.sh"
    base_hook.write_text("#!/bin/sh\ntouch \"$BASE_CALLED\"\n", encoding="utf-8")
    base_hook.chmod(0o755)
    env = {
        **os.environ,
        "SB_HOME": str(tmp_path),
        "SB_GITHUB_REPO": "colin-prologue/switchboard-mixed-canary",
        "SWITCHBOARD_CANARY_BASE_AFTER_RUN": str(base_hook),
        "BASE_CALLED": str(base_called),
    }
    (workspace / ".run" / "handoff-evidence.json").write_text(
        '{"issue": "1", "pr_number": 1, "head_sha": "abc"}', encoding="utf-8"
    )
    result = subprocess.run(
        [str(HANDOFF_HOOK)], cwd=workspace, env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert base_called.is_file()
    assert "orchestrator-owned" in result.stdout
    # evidence file untouched — the orchestrator, not the hook, consumes it
    assert (workspace / ".run" / "handoff-evidence.json").is_file()

    wrong_repo = subprocess.run(
        [str(HANDOFF_HOOK)], cwd=workspace,
        env={**env, "SB_GITHUB_REPO": "colin-prologue/other"},
        capture_output=True, text=True, check=False,
    )
    assert wrong_repo.returncode != 0
    assert "instead of" in wrong_repo.stderr


def test_stage7_procedure_pins_evidence_and_unchanged_rollback(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    source = LAUNCHER.read_text(encoding="utf-8")

    assert os.access(LAUNCHER, os.X_OK)
    assert os.access(INJECTOR, os.X_OK)
    assert os.access(HANDOFF_HOOK, os.X_OK)
    assert "cooldown_ms=300000" in source
    assert "retry_disposition=provider_wait" in source
    assert 'HALF_OPEN_COUNT' in source
    assert 'DISPATCH_COUNT' in source
    assert 'SESSION_ONE_COUNT' in source
    assert 'WORKFLOW.rollback-claude.md' in source
    assert 'RUN_MODE="default-claude"' in source
    assert "unset SWITCHBOARD_CANARY_CODEX_BIN" in source
    assert "unset SWITCHBOARD_CANARY_BASE_AFTER_RUN SWITCHBOARD_CANARY_GH_BIN" in source
    assert "SECONDS + 2700" in source
    assert "OPEN_ISSUES" in source and "OPEN_PRS" in source
    assert "circuit_recovery_complete()" in source
    assert (
        'if [ "$PHASE" != "circuit-recovery" ] || circuit_recovery_complete'
        in source
    )
    assert (
        "worker completed .*issue_identifier=$ISSUE_NUMBER .*provider_id=codex"
        in source
    )
    # issue #61: the launcher asserts the production handoff path, not the
    # legacy consumed-marker behavior.
    assert "production handoff evidence file is missing" in source
    assert "handoff evidence validated; issue moved to human-review" in source
    assert "recovery transcript lacks terminal Codex success" in source

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
