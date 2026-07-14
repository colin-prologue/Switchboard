"""Regression coverage for the checked-in Stage 5B Codex canary binding."""

from __future__ import annotations

from pathlib import Path

from orchestrator.workflow import Config, load_workflow, validate_dispatch


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / "projects" / "codex-canary" / "WORKFLOW.md"


def test_codex_canary_binding_is_strict_and_dispatchable(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    definition = load_workflow(WORKFLOW)
    cfg = Config(definition, WORKFLOW.parent)

    validate_dispatch(cfg, provider_id="codex")

    assert cfg.tracker().repo == "colin-prologue/switchboard-codex-canary"
    assert cfg.agent().max_concurrent_agents == 1
    assert cfg.codex().command.startswith("codex --ask-for-approval never")
