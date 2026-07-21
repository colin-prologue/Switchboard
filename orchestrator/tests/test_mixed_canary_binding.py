"""Regression coverage for the inert Stage 6 mixed-canary binding."""

from __future__ import annotations

from pathlib import Path

from orchestrator.workflow import Config, load_workflow, validate_dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / "projects" / "mixed-canary" / "WORKFLOW.md"


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
