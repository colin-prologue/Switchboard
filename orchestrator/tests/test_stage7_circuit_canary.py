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
    assert ".run/stage7-handoff-ready" in definition.prompt_template
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


def _prepare_handoff_workspace(
    tmp_path: Path,
    *,
    terminal_transcript: bool,
    ready_marker: bool = True,
) -> tuple[Path, dict[str, str], Path]:
    workspace = tmp_path / "14"
    workspace.mkdir()
    subprocess.run(
        ["git", "init", "-b", "switchboard/issue-14"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=workspace,
        check=True,
    )
    (workspace / "greeting.py").write_text("fixture = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "greeting.py"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "update-ref",
            "refs/remotes/origin/switchboard/issue-14",
            "HEAD",
        ],
        cwd=workspace,
        check=True,
    )
    exclude = workspace / ".git" / "info" / "exclude"
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write(".run/\n")
    transcripts = workspace / ".run" / "transcripts"
    transcripts.mkdir(parents=True)
    final_record = (
        '{"type":"turn.completed","usage":{}}\n'
        if terminal_transcript
        else '{"type":"item.completed","item":{"type":"agent_message"}}\n'
    )
    (transcripts / "codex-20260726T000000Z-1.jsonl").write_text(
        '{"type":"turn.started"}\n' + final_record,
        encoding="utf-8",
    )
    if ready_marker:
        (workspace / ".run" / "stage7-handoff-ready").touch()

    base_called = tmp_path / "base-called"
    base_hook = tmp_path / "base-after-run"
    base_hook.write_text(
        f'#!/bin/sh\ntouch "{base_called}"\n',
        encoding="utf-8",
    )
    base_hook.chmod(0o755)
    gh_calls = tmp_path / "gh-calls"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >>"$GH_CALLS"\n'
        'case "$*" in\n'
        '  *"--json number"*) printf "1\\n" ;;\n'
        '  *"--json body"*) printf "Closes #14\\n" ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "SB_HOME": str(REPO_ROOT),
        "SB_GITHUB_REPO": "colin-prologue/switchboard-mixed-canary",
        "SWITCHBOARD_CANARY_BASE_AFTER_RUN": str(base_hook),
        "SWITCHBOARD_CANARY_GH_BIN": str(fake_gh),
        "GH_CALLS": str(gh_calls),
    }
    return workspace, env, base_called


def test_stage7_handoff_hook_requires_terminal_codex_success(tmp_path: Path) -> None:
    workspace, env, base_called = _prepare_handoff_workspace(
        tmp_path,
        terminal_transcript=False,
    )

    result = subprocess.run(
        [str(HANDOFF_HOOK)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert base_called.is_file()
    assert (workspace / ".run" / "stage7-handoff-ready").is_file()
    assert not (tmp_path / "gh-calls").exists()
    assert "latest Codex turn is incomplete" in result.stdout


def test_stage7_handoff_hook_moves_only_verified_terminal_run(
    tmp_path: Path,
) -> None:
    workspace, env, base_called = _prepare_handoff_workspace(
        tmp_path,
        terminal_transcript=True,
    )

    result = subprocess.run(
        [str(HANDOFF_HOOK)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert base_called.is_file()
    assert not (workspace / ".run" / "stage7-handoff-ready").exists()
    calls = (tmp_path / "gh-calls").read_text(encoding="utf-8")
    assert calls.count("pr list") == 2
    assert (
        "issue edit 14 --repo colin-prologue/switchboard-mixed-canary "
        "--add-label status:human-review --remove-label status:in-progress"
        in calls
    )
    assert "moved to human review after terminal Codex success" in result.stdout


def test_stage7_handoff_hook_is_inert_without_ready_marker(tmp_path: Path) -> None:
    workspace, env, base_called = _prepare_handoff_workspace(
        tmp_path,
        terminal_transcript=True,
        ready_marker=False,
    )

    result = subprocess.run(
        [str(HANDOFF_HOOK)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert base_called.is_file()
    assert not (tmp_path / "gh-calls").exists()


def test_stage7_handoff_hook_rejects_dirty_workspace(tmp_path: Path) -> None:
    workspace, env, base_called = _prepare_handoff_workspace(
        tmp_path,
        terminal_transcript=True,
    )
    (workspace / "greeting.py").write_text("fixture = False\n", encoding="utf-8")

    result = subprocess.run(
        [str(HANDOFF_HOOK)],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert base_called.is_file()
    assert (workspace / ".run" / "stage7-handoff-ready").is_file()
    assert not (tmp_path / "gh-calls").exists()
    assert "handoff workspace is dirty" in result.stderr


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
    assert "terminal handoff marker was not consumed" in source
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
