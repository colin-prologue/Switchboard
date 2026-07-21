"""Regression coverage for the inert Stage 6 mixed-canary binding."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from orchestrator.workflow import Config, load_workflow, validate_dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / "projects" / "mixed-canary" / "WORKFLOW.md"
PROVISION_LABELS = REPO_ROOT / "scripts" / "provision-mixed-canary-labels.sh"
REQUIRED_LABELS = {
    "status:drafting",
    "status:triage",
    "status:todo",
    "status:in-progress",
    "status:plan-review",
    "status:human-review",
    "status:blocked",
    "status:parked",
    "gate:triage-passed",
    "agent:claude",
    "agent:codex",
    "provider:claude",
    "provider:codex",
}


def _parse_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def test_mixed_canary_binding_is_dispatchable_but_zero_codex_weight(
    monkeypatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    definition = load_workflow(WORKFLOW)
    cfg = Config(definition, WORKFLOW.parent)

    validate_dispatch(cfg, provider_id="mixed")
    mixed = cfg.mixed()

    assert cfg.tracker().repo == "colin-prologue/switchboard-mixed-canary"
    assert cfg.agent().max_concurrent_agents == 1
    assert mixed.weights == {"claude": 100, "codex": 0}
    assert mixed.max_concurrent_agents_by_provider == {"claude": 1, "codex": 1}
    assert mixed.claude.command.startswith("claude -p --verbose")
    assert mixed.codex.command.startswith("codex --ask-for-approval never")
    assert "Do not remove, replace, or add any" in definition.prompt_template
    assert "python3 -m unittest discover -s tests -v" in definition.prompt_template


def test_mixed_canary_workflow_matches_its_declared_template() -> None:
    project = WORKFLOW.parent
    env = _parse_env(project / "project.env")
    composed = WORKFLOW.read_text(encoding="utf-8")
    max_agents = next(
        line.split(":", 1)[1].strip()
        for line in composed.splitlines()
        if line.strip().startswith("max_concurrent_agents:")
    )

    assert env["SB_WORKFLOW_TEMPLATE"] == "mixed-canary"
    template = REPO_ROOT / "workflow" / f"WORKFLOW.{env['SB_WORKFLOW_TEMPLATE']}.md"
    expected = (
        template.read_text(encoding="utf-8")
        .replace("{{REPO}}", env["SB_GITHUB_REPO"])
        .replace("{{WORKSPACE_ROOT}}", env["SB_WORKSPACE_ROOT"])
        .replace("{{MAX_AGENTS}}", max_agents)
        .replace("{{CONVENTION_ROOT}}", env["SB_CONVENTION_ROOT"])
    )

    assert expected == composed


def test_mixed_canary_label_provisioning_dry_run_is_complete_and_offline(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "gh-was-called"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n', encoding="utf-8")
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(PROVISION_LABELS), "--dry-run"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "dry-run invoked gh"
    assert result.stdout.splitlines()[0] == (
        "repo: colin-prologue/switchboard-mixed-canary"
    )
    assert {
        line.removeprefix("label ")
        for line in result.stdout.splitlines()[1:]
    } == REQUIRED_LABELS


def test_mixed_canary_label_provisioning_uses_force_for_every_label(
    tmp_path: Path,
) -> None:
    arg_log = tmp_path / "gh-args"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$GH_ARG_LOG"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    env = {
        **os.environ,
        "GH_ARG_LOG": str(arg_log),
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }

    result = subprocess.run(
        ["bash", str(PROVISION_LABELS), "--repo", "acme/mixed-canary"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = arg_log.read_text(encoding="utf-8").splitlines()
    assert len(calls) == len(REQUIRED_LABELS)
    for label in REQUIRED_LABELS:
        call = next(line for line in calls if line.startswith(f"label create {label} "))
        assert "--repo acme/mixed-canary" in call
        assert call.endswith("--force")
