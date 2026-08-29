"""Tests for workflow loading and typed config views.

implements: core §17.1 (Workflow and Config Parsing test matrix), adapted for
the GitHub/Claude bindings per SPEC.md §1-2.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from orchestrator.types import (
    ClaudeConfig,
    CodexConfig,
    MixedExecutionConfig,
    WorkflowDefinition,
    WorkflowError,
)
from orchestrator.workflow import Config, load_workflow, validate_dispatch


# --- load_workflow ------------------------------------------------------------

def test_front_matter_and_body_split(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "---\n"
        "\n"
        "  Prompt body here.  \n"
    )
    defn = load_workflow(p)
    assert defn.config == {"tracker": {"kind": "github", "repo": "acme/widgets"}}
    assert defn.prompt_template == "Prompt body here."


def test_no_front_matter_whole_file_is_body(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text("  Just a prompt, no config.  \n")
    defn = load_workflow(p)
    assert defn.config == {}
    assert defn.prompt_template == "Just a prompt, no config."


def test_missing_file_raises_typed_error(tmp_path: Path):
    p = tmp_path / "does_not_exist.md"
    with pytest.raises(WorkflowError) as exc_info:
        load_workflow(p)
    assert exc_info.value.code == "missing_workflow_file"


def test_invalid_yaml_raises_typed_error(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "tracker: [unclosed\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(WorkflowError) as exc_info:
        load_workflow(p)
    assert exc_info.value.code == "workflow_parse_error"


def test_non_map_front_matter_raises_typed_error(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "- just\n"
        "- a\n"
        "- list\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(WorkflowError) as exc_info:
        load_workflow(p)
    assert exc_info.value.code == "workflow_front_matter_not_a_map"


def test_duplicate_yaml_mapping_key_raises_parse_error(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "providers:\n"
        "  claude:\n"
        "    command: first\n"
        "providers:\n"
        "  claude:\n"
        "    command: second\n"
        "---\n"
        "body\n"
    )

    with pytest.raises(WorkflowError) as exc_info:
        load_workflow(p)

    assert exc_info.value.code == "workflow_parse_error"
    assert "duplicate key" in str(exc_info.value)
    assert "providers" in str(exc_info.value)


def test_yaml_merge_override_remains_valid_for_provider_workflow(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "claude_defaults: &claude_defaults\n"
        "  kind: claude-cli\n"
        "  command: inherited\n"
        "  max_turns: 20\n"
        "providers:\n"
        "  claude:\n"
        "    <<: *claude_defaults\n"
        "    command: overridden\n"
        "---\n"
        "body\n"
    )

    cfg = Config(load_workflow(p), tmp_path)

    assert cfg.claude().command == "overridden"
    assert cfg.claude().max_turns == 20


def test_duplicate_key_inside_inline_merge_source_raises(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "providers:\n"
        "  claude:\n"
        "    <<: {kind: claude-cli, command: first, command: second}\n"
        "---\n"
        "body\n"
    )

    with pytest.raises(WorkflowError) as exc_info:
        load_workflow(p)

    assert exc_info.value.code == "workflow_parse_error"
    assert "duplicate key" in str(exc_info.value)
    assert "command" in str(exc_info.value)


def test_empty_front_matter_yields_empty_config(tmp_path: Path):
    p = tmp_path / "WORKFLOW.md"
    p.write_text("---\n---\nbody\n")
    defn = load_workflow(p)
    assert defn.config == {}
    assert defn.prompt_template == "body"


# --- Config: tracker() ---------------------------------------------------------

def test_tracker_defaults(tmp_path: Path):
    defn = WorkflowDefinition(config={"tracker": {"kind": "github", "repo": "acme/widgets"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    t = cfg.tracker()
    assert t.kind == "github"
    assert t.repo == "acme/widgets"
    assert t.endpoint == "https://api.github.com/graphql"
    assert t.required_labels == []
    # SPEC.md §2 binding: triage is active (AgDR-006); issue-closed is the
    # ONLY terminal condition — a status:* label must never be terminal, or
    # a stray status:done on an OPEN issue would destroy its workspace.
    assert t.active_states == ["triage", "todo", "in progress"]
    assert t.terminal_states == ["closed"]


def test_tracker_handoff_label_defaults_to_human_review(tmp_path: Path):
    """Absent config, the terminal handoff still lands on the Gate C label.

    The default is what keeps every pre-stance binding behaving identically:
    only a stance that explicitly opts into an agent QA state changes it.
    """
    defn = WorkflowDefinition(config={"tracker": {"kind": "github", "repo": "acme/widgets"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.tracker().handoff_label == "status:human-review"


def test_tracker_handoff_label_override_targets_an_active_qa_state(tmp_path: Path):
    """An autonomous stance points the handoff at a state it also dispatches.

    If `handoff_label` named a state absent from `active_states`, a validated
    handoff would park the issue forever — so the two are set together, and
    this pins that pairing.
    """
    defn = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "github",
                "repo": "acme/widgets",
                "active_states": ["todo", "in progress", "review"],
                "handoff_label": "status:review",
            }
        },
        prompt_template="",
    )
    t = Config(defn, tmp_path).tracker()
    assert t.handoff_label == "status:review"
    assert t.handoff_label.removeprefix("status:").replace("-", " ") in t.active_states


def test_tracker_handoff_label_rejects_a_non_dispatchable_target(tmp_path: Path):
    """A non-default handoff target absent from active_states must not load.

    The transition itself would succeed, so the failure is silent: every
    completed ticket lands on a label the eligibility filter excludes and parks
    forever with neither QA nor a human handoff. Fail at load instead.
    """
    defn = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "github",
                "repo": "acme/widgets",
                "active_states": ["todo", "in progress"],
                "handoff_label": "status:review",
            }
        },
        prompt_template="",
    )
    with pytest.raises(WorkflowError) as exc:
        Config(defn, tmp_path).tracker()
    assert exc.value.code == "invalid_handoff_label"
    assert "active_states" in str(exc.value)


def test_tracker_handoff_label_rejects_a_non_status_label(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "github",
                "repo": "acme/widgets",
                "active_states": ["todo", "review"],
                "handoff_label": "review",
            }
        },
        prompt_template="",
    )
    with pytest.raises(WorkflowError) as exc:
        Config(defn, tmp_path).tracker()
    assert exc.value.code == "invalid_handoff_label"


def test_tracker_default_handoff_label_is_exempt_from_the_active_check(tmp_path: Path):
    """The default is a HUMAN gate — gated stances keep it out of active_states
    deliberately, so validating it would break every pre-stance binding."""
    defn = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "github",
                "repo": "acme/widgets",
                "active_states": ["triage", "todo", "in progress"],
            }
        },
        prompt_template="",
    )
    t = Config(defn, tmp_path).tracker()
    assert t.handoff_label == "status:human-review"
    assert "human review" not in t.active_states


def test_tracker_handoff_label_blank_falls_back_to_default(tmp_path: Path):
    """An empty string is a composition accident, not an intent to unset."""
    defn = WorkflowDefinition(
        config={"tracker": {"kind": "github", "repo": "acme/widgets", "handoff_label": "   "}},
        prompt_template="",
    )
    assert Config(defn, tmp_path).tracker().handoff_label == "status:human-review"


def test_tracker_api_key_dollar_var_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    defn = WorkflowDefinition(config={"tracker": {"kind": "github", "repo": "acme/widgets"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.tracker().api_key == "secret-token"


def test_tracker_api_key_missing_env_resolves_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    defn = WorkflowDefinition(config={"tracker": {"kind": "github", "repo": "acme/widgets"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.tracker().api_key == ""


def test_tracker_states_and_labels_normalized(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "github",
                "repo": "acme/widgets",
                "required_labels": ["  Ready  ", "URGENT"],
                "active_states": ["Todo", " In Progress "],
                "terminal_states": ["Done", "Closed"],
            }
        },
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    t = cfg.tracker()
    assert t.required_labels == ["ready", "urgent"]
    assert t.active_states == ["todo", "in progress"]
    assert t.terminal_states == ["done", "closed"]


def test_tracker_repo_absent_defaults_empty(tmp_path: Path):
    defn = WorkflowDefinition(config={"tracker": {"kind": "github"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.tracker().repo == ""


# --- Config: polling_interval_ms() ---------------------------------------------

def test_polling_interval_default(tmp_path: Path):
    defn = WorkflowDefinition(config={}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.polling_interval_ms() == 30000


def test_polling_interval_override(tmp_path: Path):
    defn = WorkflowDefinition(config={"polling": {"interval_ms": 5000}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.polling_interval_ms() == 5000


# --- Config: workspace_root() ---------------------------------------------------

def test_workspace_root_default(tmp_path: Path):
    from orchestrator.types import DEFAULT_WORKSPACE_ROOT

    defn = WorkflowDefinition(config={}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.workspace_root() == Path(DEFAULT_WORKSPACE_ROOT)


def test_workspace_root_tilde_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    defn = WorkflowDefinition(config={"workspace": {"root": "~/ws"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.workspace_root() == (tmp_path / "ws")


def test_workspace_root_dollar_var_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "from-env"))
    defn = WorkflowDefinition(config={"workspace": {"root": "$WORKSPACE_ROOT"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.workspace_root() == (tmp_path / "from-env")


def test_workspace_root_relative_resolves_against_workflow_dir(tmp_path: Path):
    workflow_dir = tmp_path / "project"
    workflow_dir.mkdir()
    defn = WorkflowDefinition(config={"workspace": {"root": "workspaces"}}, prompt_template="")
    cfg = Config(defn, workflow_dir)
    assert cfg.workspace_root() == (workflow_dir / "workspaces")
    assert cfg.workspace_root().is_absolute()


# --- Config: hooks() -------------------------------------------------------------

def test_hooks_defaults(tmp_path: Path):
    defn = WorkflowDefinition(config={}, prompt_template="")
    cfg = Config(defn, tmp_path)
    h = cfg.hooks()
    assert h.after_create is None
    assert h.before_run is None
    assert h.after_run is None
    assert h.before_remove is None
    assert h.timeout_ms == 60000


def test_hooks_scripts_and_timeout(tmp_path: Path):
    defn = WorkflowDefinition(
        config={"hooks": {"after_create": "echo hi", "timeout_ms": 5000}},
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    h = cfg.hooks()
    assert h.after_create == "echo hi"
    assert h.timeout_ms == 5000


def test_hooks_invalid_timeout_raises_at_access(tmp_path: Path):
    defn = WorkflowDefinition(config={"hooks": {"timeout_ms": -5}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        cfg.hooks()
    assert exc_info.value.code == "workflow_parse_error"


def test_hooks_non_integer_timeout_raises(tmp_path: Path):
    defn = WorkflowDefinition(config={"hooks": {"timeout_ms": "soon"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        cfg.hooks()
    assert exc_info.value.code == "workflow_parse_error"


# --- Config: agent() -------------------------------------------------------------

def test_agent_defaults(tmp_path: Path):
    defn = WorkflowDefinition(config={}, prompt_template="")
    cfg = Config(defn, tmp_path)
    a = cfg.agent()
    assert a.max_concurrent_agents == 10
    assert a.max_turns == 20
    assert a.max_retry_backoff_ms == 300000
    assert a.max_concurrent_agents_by_state == {}
    assert a.max_sessions_per_issue == 3


def test_agent_by_state_normalization_and_invalid_entries_ignored(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "agent": {
                "max_concurrent_agents_by_state": {
                    "Todo": 2,
                    "IN PROGRESS": 3,
                    "bad": -1,
                    "also_bad": "nope",
                    "zero": 0,
                }
            }
        },
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    by_state = cfg.agent().max_concurrent_agents_by_state
    assert by_state == {"todo": 2, "in progress": 3}


def test_agent_invalid_max_turns_raises(tmp_path: Path):
    defn = WorkflowDefinition(config={"agent": {"max_turns": 0}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        cfg.agent()
    assert exc_info.value.code == "workflow_parse_error"


# --- Config: claude() --------------------------------------------------------------

def test_claude_defaults(tmp_path: Path):
    defn = WorkflowDefinition(config={}, prompt_template="")
    cfg = Config(defn, tmp_path)
    c = cfg.claude()
    assert c.command == "claude -p --verbose --output-format stream-json"
    assert c.max_turns == 20
    assert c.max_budget_usd is None
    assert c.turn_timeout_ms == 3600000
    assert c.read_timeout_ms == 5000
    assert c.stall_timeout_ms == 300000


def test_claude_command_preserved_as_shell_string(tmp_path: Path):
    cmd = "claude -p --output-format stream-json --allowedTools 'Bash(git:*)'"
    defn = WorkflowDefinition(
        config={"providers": {"claude": {"kind": "claude-cli", "command": cmd}}},
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    assert cfg.claude().command == cmd


def test_claude_max_budget_usd_float(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "providers": {"claude": {"kind": "claude-cli", "max_budget_usd": 5}}
        },
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    assert cfg.claude().max_budget_usd == 5.0
    assert isinstance(cfg.claude().max_budget_usd, float)


def test_providers_claude_parses_to_existing_typed_config(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "providers": {
                "claude": {
                    "kind": "claude-cli",
                    "command": "claude -p --output-format stream-json",
                    "max_turns": 40,
                    "max_budget_usd": 7,
                    "turn_timeout_ms": 1234,
                    "read_timeout_ms": 2345,
                    "stall_timeout_ms": 3456,
                }
            }
        },
        prompt_template="",
    )

    cfg = Config(defn, tmp_path)
    assert cfg.claude() == ClaudeConfig(
        command="claude -p --output-format stream-json",
        max_turns=40,
        max_budget_usd=7.0,
        turn_timeout_ms=1234,
        read_timeout_ms=2345,
        stall_timeout_ms=3456,
    )


def test_providers_codex_parses_to_typed_config(tmp_path: Path):
    cfg = Config(
        WorkflowDefinition(
            config={
                "providers": {
                    "codex": {
                        "kind": "codex-cli",
                        "command": "codex --sandbox workspace-write",
                        "turn_timeout_ms": 1234,
                        "read_timeout_ms": 2345,
                        "stall_timeout_ms": 3456,
                    }
                }
            },
            prompt_template="",
        ),
        tmp_path,
    )

    assert cfg.codex() == CodexConfig(
        command="codex --sandbox workspace-write",
        turn_timeout_ms=1234,
        read_timeout_ms=2345,
        stall_timeout_ms=3456,
    )


def test_providers_codex_uses_safe_adapter_defaults(tmp_path: Path):
    cfg = Config(
        WorkflowDefinition(
            config={"providers": {"codex": {"kind": "codex-cli"}}},
            prompt_template="",
        ),
        tmp_path,
    )

    assert cfg.codex() == CodexConfig()


@pytest.mark.parametrize(
    ("config", "error_code"),
    [
        ({"codex": {"command": "codex"}}, "missing_provider_config"),
        (
            {
                "providers": {
                    "claude": {"kind": "claude-cli"},
                    "codex": {"kind": "codex-cli"},
                }
            },
            "unsupported_provider_id",
        ),
        (
            {
                "claude": {"command": "claude -p"},
                "providers": {"codex": {"kind": "codex-cli"}},
            },
            "unsupported_provider_id",
        ),
        (
            {"providers": {"codex": {"kind": "openai-api"}}},
            "unsupported_provider_kind",
        ),
        (
            {"providers": {"codex": {"kind": "codex-cli", "unknown": 1}}},
            "workflow_parse_error",
        ),
    ],
)
def test_codex_config_rejects_legacy_mixed_or_malformed_forms(
    tmp_path: Path,
    config: dict,
    error_code: str,
) -> None:
    cfg = Config(WorkflowDefinition(config=config, prompt_template=""), tmp_path)

    with pytest.raises(WorkflowError) as exc_info:
        cfg.codex()

    assert exc_info.value.code == error_code


@pytest.mark.parametrize(
    "config",
    [
        # Legacy-only: the shape AgDR-017 kept alive and AgDR-2026-08-29-retire-the-legacy-claude-block retired.
        {"claude": {"command": "claude -p"}},
        # Legacy beside the envelope: what the dual-read used to reconcile.
        {
            "claude": {"command": "claude -p"},
            "providers": {
                "claude": {
                    "kind": "claude-cli",
                    "command": "claude -p",
                    "max_turns": 20,
                }
            },
        },
        # Legacy beside a *conflicting* envelope: this used to be the only
        # rejected combination (conflicting_provider_config). It is now
        # rejected for the same reason as the equivalent one — the block
        # itself, not the disagreement.
        {
            "claude": {"command": "claude -p", "max_turns": 20},
            "providers": {
                "claude": {
                    "kind": "claude-cli",
                    "command": "claude -p",
                    "max_turns": 30,
                }
            },
        },
        # A non-map legacy block used to be coerced to defaults, not refused.
        {"claude": "not-a-map"},
    ],
)
def test_legacy_top_level_claude_block_is_rejected(tmp_path: Path, config: dict):
    """AgDR-2026-08-29-retire-the-legacy-claude-block: the legacy shape is refused by name, never read.

    The message must name the migration — a bare parse error would leave an
    operator guessing that a block which worked yesterday is now a typo.
    """
    cfg = Config(WorkflowDefinition(config=config, prompt_template=""), tmp_path)

    with pytest.raises(WorkflowError) as exc_info:
        cfg.claude()

    assert exc_info.value.code == "unsupported_provider_id"
    message = str(exc_info.value)
    assert "providers.claude" in message
    assert "kind: claude-cli" in message


def test_legacy_claude_coercions_are_gone_with_the_legacy_block(tmp_path: Path):
    """The lenient parse existed only for the legacy block; both are gone.

    A non-string command and a boolean budget silently became defaults under
    the legacy block. In the envelope they are parse errors.
    """
    cfg = Config(
        WorkflowDefinition(
            config={
                "providers": {
                    "claude": {
                        "kind": "claude-cli",
                        "command": 42,
                        "max_budget_usd": True,
                    }
                }
            },
            prompt_template="",
        ),
        tmp_path,
    )

    with pytest.raises(WorkflowError) as exc_info:
        cfg.claude()

    assert exc_info.value.code == "workflow_parse_error"


@pytest.mark.parametrize(
    "override",
    [
        {"command": 42},
        {"max_budget_usd": True},
        {"max_budget_usd": "five"},
        {"max_buget_usd": 5},
    ],
)
def test_provider_claude_rejects_malformed_or_unknown_fields(
    tmp_path: Path,
    override: dict,
):
    provider = {"kind": "claude-cli", **override}
    cfg = Config(
        WorkflowDefinition(
            config={"providers": {"claude": provider}},
            prompt_template="",
        ),
        tmp_path,
    )

    with pytest.raises(WorkflowError) as exc_info:
        cfg.claude()

    assert exc_info.value.code == "workflow_parse_error"
    assert "providers.claude" in str(exc_info.value)


def test_no_execution_block_still_resolves_to_defaults(tmp_path: Path):
    """AgDR-2026-08-29-retire-the-legacy-claude-block removed a config *shape*, not the optionality of the block.

    A workflow with no `providers:` key is the absence of configuration, which
    has always meant "take the defaults" — distinct from the legacy shape. It
    must resolve to exactly what an all-default envelope resolves to.
    """
    empty = Config(WorkflowDefinition(config={}, prompt_template=""), tmp_path)
    envelope = Config(
        WorkflowDefinition(
            config={"providers": {"claude": {"kind": "claude-cli"}}},
            prompt_template="",
        ),
        tmp_path,
    )

    assert empty.claude() == envelope.claude()


@pytest.mark.parametrize(
    ("providers", "error_code"),
    [
        ([], "workflow_parse_error"),
        ({}, "missing_provider_config"),
        ({"claude": []}, "workflow_parse_error"),
        ({"claude": {}}, "unsupported_provider_kind"),
        (
            {
                "claude": {"kind": "claude-cli"},
                "codex": {"kind": "codex-cli"},
            },
            "unsupported_provider_id",
        ),
        (
            {"claude": {"kind": "not-claude-cli"}},
            "unsupported_provider_kind",
        ),
    ],
)
def test_invalid_provider_envelopes_fail(
    tmp_path: Path,
    providers,
    error_code: str,
):
    cfg = Config(
        WorkflowDefinition(config={"providers": providers}, prompt_template=""),
        tmp_path,
    )

    with pytest.raises(WorkflowError) as exc_info:
        cfg.claude()

    assert exc_info.value.code == error_code


def test_validate_dispatch_accepts_new_provider_envelope(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "tracker": {
                "kind": "github",
                "repo": "acme/widgets",
                "api_key": "literal-token",
            },
            "providers": {
                "claude": {"kind": "claude-cli", "command": "claude -p"}
            },
        },
        prompt_template="",
    )

    validate_dispatch(Config(defn, tmp_path))


def test_validate_dispatch_accepts_codex_only_when_explicitly_selected(
    tmp_path: Path,
) -> None:
    cfg = Config(
        WorkflowDefinition(
            config={
                "tracker": {
                    "kind": "github",
                    "repo": "acme/widgets",
                    "api_key": "literal-token",
                },
                "providers": {"codex": {"kind": "codex-cli"}},
            },
            prompt_template="",
        ),
        tmp_path,
    )

    validate_dispatch(cfg, provider_id="codex")
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "unsupported_provider_id"


def test_mixed_mode_parses_complete_envelope_and_validates_dispatch(
    tmp_path: Path,
) -> None:
    cfg = Config(
        WorkflowDefinition(
            config={
                "tracker": {
                    "kind": "github",
                    "repo": "acme/widgets",
                    "api_key": "literal-token",
                },
                "agent": {
                    "max_concurrent_agents": 4,
                    "max_concurrent_agents_by_provider": {
                        "claude": 4,
                        "codex": 1,
                    },
                },
                "providers": {
                    "claude": {"kind": "claude-cli", "command": "claude -p"},
                    "codex": {"kind": "codex-cli", "command": "codex"},
                },
                "routing": {"weights": {"claude": 100, "codex": 0}},
            },
            prompt_template="",
        ),
        tmp_path,
    )

    mixed = cfg.mixed()

    assert mixed == MixedExecutionConfig(
        claude=ClaudeConfig(command="claude -p", max_turns=20, max_budget_usd=None,
                            turn_timeout_ms=3600000, read_timeout_ms=5000,
                            stall_timeout_ms=300000),
        codex=CodexConfig(command="codex"),
        weights={"claude": 100, "codex": 0},
        max_concurrent_agents_by_provider={"claude": 4, "codex": 1},
    )
    validate_dispatch(cfg, provider_id="mixed")


def test_mixed_mode_uses_global_cap_when_provider_caps_are_omitted(
    tmp_path: Path,
) -> None:
    cfg = Config(
        WorkflowDefinition(
            config={
                "providers": {
                    "claude": {"kind": "claude-cli"},
                    "codex": {"kind": "codex-cli"},
                },
                "routing": {"weights": {"claude": 1, "codex": 1}},
            },
            prompt_template="",
        ),
        tmp_path,
    )

    assert cfg.mixed().max_concurrent_agents_by_provider == {}


@pytest.mark.parametrize(
    ("config", "error_code"),
    [
        (
            {
                "providers": {"claude": {"kind": "claude-cli"}},
                "routing": {"weights": {"claude": 1, "codex": 1}},
            },
            "missing_provider_config",
        ),
        (
            {
                "claude": {"command": "claude -p"},
                "providers": {
                    "claude": {"kind": "claude-cli"},
                    "codex": {"kind": "codex-cli"},
                },
                "routing": {"weights": {"claude": 1, "codex": 1}},
            },
            "unsupported_provider_id",
        ),
        (
            {
                "providers": {
                    "claude": {"kind": "claude-cli"},
                    "codex": {"kind": "codex-cli"},
                },
            },
            "missing_routing_config",
        ),
        (
            {
                "providers": {
                    "claude": {"kind": "claude-cli"},
                    "codex": {"kind": "codex-cli"},
                },
                "routing": {"weights": {"claude": 0, "codex": 0}},
            },
            "workflow_parse_error",
        ),
        (
            {
                "agent": {
                    "max_concurrent_agents": 2,
                    "max_concurrent_agents_by_provider": {"codex": 3},
                },
                "providers": {
                    "claude": {"kind": "claude-cli"},
                    "codex": {"kind": "codex-cli"},
                },
                "routing": {"weights": {"claude": 1, "codex": 1}},
            },
            "workflow_parse_error",
        ),
    ],
)
def test_mixed_mode_rejects_incomplete_or_unsafe_config(
    tmp_path: Path,
    config: dict,
    error_code: str,
) -> None:
    cfg = Config(WorkflowDefinition(config=config, prompt_template=""), tmp_path)

    with pytest.raises(WorkflowError) as exc_info:
        cfg.mixed()

    assert exc_info.value.code == error_code


# --- validate_dispatch() -----------------------------------------------------------

def _cfg_with_tracker(tmp_path: Path, **tracker_overrides) -> Config:
    tracker = {"kind": "github", "repo": "acme/widgets", "api_key": "literal-token"}
    tracker.update(tracker_overrides)
    defn = WorkflowDefinition(config={"tracker": tracker}, prompt_template="")
    return Config(defn, tmp_path)


def test_validate_dispatch_ok(tmp_path: Path):
    cfg = _cfg_with_tracker(tmp_path)
    validate_dispatch(cfg)  # should not raise


def test_validate_dispatch_unsupported_tracker_kind(tmp_path: Path):
    cfg = _cfg_with_tracker(tmp_path, kind="linear")
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "unsupported_tracker_kind"


def test_validate_dispatch_missing_tracker_kind(tmp_path: Path):
    defn = WorkflowDefinition(config={"tracker": {"repo": "acme/widgets", "api_key": "x"}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "unsupported_tracker_kind"


def test_validate_dispatch_missing_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # "No credentials" means neither the dogfood token NOR the App-path env
    # (validate_dispatch accepts either). Clear both, or this fails whenever it
    # runs in an App-credentialed shell (the worker environment exports SB_APP_*).
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for _app_var in (
        "SB_APP_ID",
        "SB_APP_INSTALLATION_ID",
        "SB_APP_PRIVATE_KEY_FILE",
        "SB_APP_BOT_LOGIN",
        "SB_APP_BOT_USER_ID",
    ):
        monkeypatch.delenv(_app_var, raising=False)
    defn = WorkflowDefinition(
        config={"tracker": {"kind": "github", "repo": "acme/widgets", "api_key": "$GITHUB_TOKEN"}},
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "missing_tracker_api_key"


def test_validate_dispatch_missing_repo(tmp_path: Path):
    cfg = _cfg_with_tracker(tmp_path, repo="")
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "missing_tracker_repo"


def test_validate_dispatch_repo_not_owner_name_shaped(tmp_path: Path):
    cfg = _cfg_with_tracker(tmp_path, repo="not-shaped")
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "missing_tracker_repo"


def test_validate_dispatch_empty_claude_command(tmp_path: Path):
    defn = WorkflowDefinition(
        config={
            "tracker": {"kind": "github", "repo": "acme/widgets", "api_key": "literal-token"},
            "providers": {"claude": {"kind": "claude-cli", "command": "   "}},
        },
        prompt_template="",
    )
    cfg = Config(defn, tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "workflow_parse_error"


# --- real workflow file: workflow/WORKFLOW.base.md -----------------------------

def test_real_workflow_base_file_prompt_body_loads():
    """workflow/WORKFLOW.base.md is a real, checked-in workflow file.

    NOTE: its front matter contains unquoted `{{MAX_AGENTS}}` (a Liquid-style
    placeholder meant to be substituted at registration time, before Symphony
    ever loads the file). As committed, PyYAML's safe_load cannot parse this:
    `{{MAX_AGENTS}}` parses as a flow-mapping key (`{MAX_AGENTS}`) used as a
    dict value, which is unhashable, raising `yaml.constructor.ConstructorError`.
    This is a kit bug in the base template (the placeholder must be quoted,
    e.g. `"{{MAX_AGENTS}}"`, to be valid YAML prior to substitution) rather
    than a loader defect, so we do not assert a full front-matter parse here.
    We do assert that the file exists and that a substituted copy (as
    register-project.sh would actually produce) loads cleanly end-to-end.
    """
    real_path = Path(__file__).resolve().parents[2] / "workflow" / "WORKFLOW.base.md"
    assert real_path.exists()

    raw_text = real_path.read_text(encoding="utf-8")
    # Sanity-check our documented kit bug still reproduces against the
    # checked-in file, so this test fails loudly if the file is ever fixed
    # (at which point the assertion below should be replaced with a real
    # load_workflow() call).
    import yaml

    front_matter_text = raw_text.split("---", 2)[1]
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(front_matter_text)


def test_real_workflow_base_file_loads_after_placeholder_substitution(tmp_path: Path):
    """Simulates register-project.sh substitution, then exercises the real loader."""
    real_path = Path(__file__).resolve().parents[2] / "workflow" / "WORKFLOW.base.md"
    text = real_path.read_text(encoding="utf-8")
    substituted = (
        text.replace("{{REPO}}", "acme/widgets")
        .replace("{{WORKSPACE_ROOT}}", "/tmp/symphony_workspaces/acme-widgets")
        .replace("{{MAX_AGENTS}}", "10")
        .replace("{{CONVENTION_ROOT}}", "")
        .replace("{{OPERATOR_LOGIN_YAML}}", "")
        .replace("{{REVIEW_BOT_YAML}}", "")
    )
    p = tmp_path / "WORKFLOW.md"
    p.write_text(substituted)

    defn = load_workflow(p)
    cfg = Config(defn, tmp_path)

    tracker = cfg.tracker()
    assert tracker.kind == "github"
    assert tracker.repo == "acme/widgets"
    assert "triage" in tracker.active_states  # verifier sessions are dispatchable

    agent = cfg.agent()
    assert agent.max_concurrent_agents == 10

    claude_cfg = cfg.claude()
    assert claude_cfg.max_budget_usd == 5.0

    assert "issue.identifier" in defn.prompt_template


# --- status:decision is a gate BY OMISSION (issue #55) ------------------------
#
# Adding a gate state must cost zero config: `active_states` is an allowlist, so
# a state simply absent from it is never dispatched. This test is the regression
# that fails if someone "helpfully" adds "decision" to the list.

def test_decision_is_not_an_active_state(tmp_path: Path):
    real_path = Path(__file__).resolve().parents[2] / "workflow" / "WORKFLOW.base.md"
    substituted = (
        real_path.read_text(encoding="utf-8")
        .replace("{{REPO}}", "acme/widgets")
        .replace("{{WORKSPACE_ROOT}}", "/tmp/symphony_workspaces/acme-widgets")
        .replace("{{MAX_AGENTS}}", "10")
        .replace("{{CONVENTION_ROOT}}", "")
        .replace("{{OPERATOR_LOGIN_YAML}}", "")
        .replace("{{REVIEW_BOT_YAML}}", "")
    )
    p = tmp_path / "WORKFLOW.md"
    p.write_text(substituted)

    cfg = Config(load_workflow(p), tmp_path)
    assert "decision" not in cfg.tracker().active_states
    # The whole allowlist, pinned: gates are the states NOT here. `fail review`
    # joined it in #31 — the post-failure verifier is DISPATCHED, so a state the
    # prompt names is a gate unless it is listed here.
    assert cfg.tracker().active_states == [
        "triage", "todo", "in progress", "fail review"]


def test_active_states_line_is_byte_identical_in_base_and_composed():
    """#55 added a gate state without touching this line. Both files must still
    carry the exact same `active_states` declaration."""
    repo_root = Path(__file__).resolve().parents[2]
    expected = '  active_states: ["triage", "todo", "in progress", "fail review"]'
    for rel in ("workflow/WORKFLOW.base.md", "projects/switchboard-self/WORKFLOW.md"):
        lines = (repo_root / rel).read_text(encoding="utf-8").splitlines()
        matches = [l for l in lines if l.strip().startswith("active_states:")]
        assert matches == [expected], f"{rel}: active_states drifted -> {matches}"


# --- base <-> composed conformance (issue #44) --------------------------------
#
# register-project.sh composes projects/switchboard-self/WORKFLOW.md from
# workflow/WORKFLOW.base.md by sed-substituting the ALL-CAPS placeholders. That
# script is outside the worker allowlist, so agents edit BOTH files by hand — and
# hand-edits drift. This test performs the same substitution in-process and
# asserts the tracked composed file matches byte-for-byte, so any edit to one file
# without the mirror is a red suite (no human memory or script run required).


def _parse_env(path: Path) -> dict[str, str]:
    """Parse a project.env (KEY=value lines; ignore comments/blanks).

    Values may be shell-quoted — register-project.sh single-quotes every value
    it persists, because run-project.sh SOURCES this file — so the quotes are
    stripped here the way sourcing would.
    """
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def _yaml_login(value: str) -> str:
    """The empty-stays-`[]` derivation the three recomposers share (issue #171).

    `register-project.sh`, `verify-setup.sh` and `freshness-preflight.sh` each
    carry their own copy in shell; this is the fourth. Unset composes to nothing
    between the brackets — `[]`, never `[""]`, which would match a bot named
    empty-string.
    """
    return f'"{value}"' if value else ""


def test_base_and_composed_workflow_are_in_sync():
    repo_root = Path(__file__).resolve().parents[2]
    base = repo_root / "workflow" / "WORKFLOW.base.md"
    proj = repo_root / "projects" / "switchboard-self"
    composed = proj / "WORKFLOW.md"

    env = _parse_env(proj / "project.env")
    composed_text = composed.read_text(encoding="utf-8")

    # {{MAX_AGENTS}} is the one substitution value register-project.sh does not
    # persist to project.env, so source it from the composed file's rendered
    # scalar. (Circular only for that one number; the body-text drift this test
    # guards is unaffected — those are literals in both files, not placeholders.)
    max_agents = next(
        line.split(":", 1)[1].strip()
        for line in composed_text.splitlines()
        if line.strip().startswith("max_concurrent_agents:")
    )

    substituted = (
        base.read_text(encoding="utf-8")
        .replace("{{REPO}}", env["SB_GITHUB_REPO"])
        .replace("{{WORKSPACE_ROOT}}", env["SB_WORKSPACE_ROOT"])
        .replace("{{MAX_AGENTS}}", max_agents)
        .replace("{{CONVENTION_ROOT}}", env["SB_CONVENTION_ROOT"])
        .replace(
            "{{OPERATOR_LOGIN_YAML}}",
            _yaml_login(env.get("SB_OPERATOR_LOGIN", "")),
        )
        .replace(
            "{{REVIEW_BOT_YAML}}", _yaml_login(env.get("SB_REVIEW_BOT", ""))
        )
    )

    assert substituted == composed_text, (
        "workflow/WORKFLOW.base.md and projects/switchboard-self/WORKFLOW.md have "
        "drifted. Edit BOTH (register-project.sh is outside the worker allowlist)."
    )


def test_workflow_prompt_pins_in_brief_block():
    """The plain-language block must reach the agent through the prompt itself.

    Pinned in BOTH files: the base template and the composed mirror. The
    sync test above proves they match; this test proves the content is
    actually there, so a well-intentioned "simplification" of either file
    cannot silently drop the requirement while staying in sync.
    """
    repo_root = Path(__file__).resolve().parents[2]
    for rel in ("workflow/WORKFLOW.base.md", "projects/switchboard-self/WORKFLOW.md"):
        text = (repo_root / rel).read_text(encoding="utf-8")
        assert "## In brief" in text, f"{rel}: block heading absent"
        assert "**What this does:**" in text, f"{rel}: first field absent"
        assert "**What could be wrong:**" in text, f"{rel}: second field absent"
        # The PR-handoff step must keep Closes #N ahead of the block. The
        # orchestrator resolves the issue link through GitHub's closing
        # references, which match anywhere in the body — presence is what the
        # handoff check enforces; staying first is convention only, so the
        # line stays visible and doesn't get edited away.
        assert "Keep the `Closes #N` line first" in text, f"{rel}: ordering rule absent"
        # Gate C consequence must be stated where the agent reads it.
        assert "is incomplete at the merge gate" in text, f"{rel}: gate consequence absent"
        # The closing reference is instructed, not assumed — handoff.py rejects
        # a PR that does not close this issue with `pr_linkage_missing`.
        assert "a literal\n   closing reference, not prose that mentions the issue" in text, (
            f"{rel}: Closes #N instruction absent"
        )
        # Three of the five verdict routes carry the block (NEEDS WORK, NEEDS
        # DECISION, SPLIT); PASS and the unchanged-body fast-path deliberately
        # do not. Each of the three is pinned by a string that occurs ONCE in
        # the file and only inside its own route, so deleting any single
        # insertion turns this test red — the sync test alone cannot catch a
        # deletion mirrored across both files.
        #
        # On a verdict comment the block is THIRD: the `## Triage verdict`
        # heading and the machine-parsed `body-sha1:` line keep lines 1-2.
        # Counted, not just present: one occurrence per block-carrying route,
        # so losing the rule from any single route is red.
        assert text.count("the machine-read hash stays second") == 3, (
            f"{rel}: verdict-block placement rule must appear once per "
            f"block-carrying route, found {text.count('the machine-read hash stays second')}"
        )
        # NEEDS WORK — lives only in that route's insertion, not the step-8
        # PR-handoff insertion.
        assert (
            "the single finding the author has the strongest\n  > case to push back on"
            in text
        ), f"{rel}: NEEDS WORK In brief block absent"
        # NEEDS DECISION — the route main added; its second field asks how the
        # decision request's own framing could be wrong.
        assert (
            "an option you left off, or a two-way choice that is really three" in text
        ), f"{rel}: NEEDS DECISION In brief block absent"
        # SPLIT-verdict child issues must also open with the block.
        assert "each\n  body opens with the `## In brief` block" in text, (
            f"{rel}: SPLIT child-issue body block instruction absent"
        )
        # The SPLIT verdict's own `## Triage verdict` comment must also carry
        # the block, not just the child bodies — this string lives ONLY inside
        # that SPLIT-comment insertion (distinct from the NEEDS WORK and NEEDS
        # DECISION blocks above and the SPLIT child-body clause just checked),
        # so a regression that drops just this insertion turns this test red.
        assert "the split decision most likely to be wrong" in text, (
            f"{rel}: SPLIT triage-verdict comment In brief block absent"
        )
        # The two mechanical routes stay block-free on purpose. Without this,
        # nothing distinguishes "deliberately excluded" from "forgotten", and a
        # later session would add ceremony back to a one-line PASS.
        assert "The fast-path comment carries no `## In brief` block" in text, (
            f"{rel}: PASS/fast-path exclusion rationale absent"
        )


# --- decision-record naming (self/.decisions) ----------------------------------
#
# Records used to be numbered, and parallel worker sessions each picked "next free
# AgDR number on their own branch" — so two branches minted the same number and
# both merged green (each passed in isolation). Issue #154 removed the allocator
# rather than guarding it: new records are `AgDR-YYYY-MM-DD-<slug>.md`, which
# cannot collide unless two sessions decide the same thing on the same day, and
# that collision is a real duplicate worth reporting.
#
# Legacy numbered records are frozen, not renamed (the seam is self-describing: a
# number means pre-changeover), so this test carries both forms and keeps the
# uniqueness backstop for each in its own key space.

# Every numbered record on `main` at the #154 changeover. Frozen: these names are
# cited from prose, code comments, and docstrings across the repo, so renaming one
# silently breaks references no test would catch.
LEGACY_NUMBERED_RECORDS = frozenset(
    {
        "ADR-000-repair-and-rederivation.md",
        "AgDR-001-python-asyncio-single-process.md",
        "AgDR-002-session-cap-parking.md",
        "AgDR-003-reload-mtime-polling.md",
        "AgDR-004-permission-posture.md",
        "AgDR-005-role-pinned-sessions.md",
        "AgDR-006-triage-as-active-state.md",
        "AgDR-007-triage-pass-agent-promotion.md",
        "AgDR-008-durable-park-label.md",
        "AgDR-009-github-app-identity.md",
        "AgDR-010-in-progress-claim-visibility.md",
        "AgDR-011-config-driven-dispatch-marker-guard.md",
        "AgDR-012-graph-review-phasing.md",
        "AgDR-013-worker-turn-budget-cold-start.md",
        "AgDR-014-drafting-quality-at-scaffold-and-rubric.md",
        "AgDR-015-triage-native-dependency-edge-check.md",
        "AgDR-016-subscription-first-codex-auth.md",
        "AgDR-017-dual-read-provider-config.md",
        "AgDR-018-dispatch-time-runner-selection.md",
        "AgDR-019-standalone-codex-cli-adapter.md",
        "AgDR-020-opt-in-codex-process-mode.md",
        "AgDR-021-codex-canary-python-command.md",
        "AgDR-022-native-terminal-codex-canary-launch.md",
        "AgDR-023-stage6-mixed-routing-policy.md",
        "AgDR-024-deterministic-nonzero-codex-canary.md",
        "AgDR-025-provider-observability-taxonomy.md",
        "AgDR-026-provider-circuit-and-no-retry-burn.md",
        "AgDR-027-incomplete-turn-continuation.md",
        "AgDR-028-orchestrator-owned-terminal-handoff.md",
        "AgDR-029-self-pilot-separate-binding.md",
        "AgDR-030-codex-error-events-are-non-terminal.md",
        "AgDR-031-needs-decision-verdict-and-body-hash-fastpath.md",
        "AgDR-032-is-error-outcome-gate.md",
        "AgDR-033-per-role-session-budgets.md",
        "AgDR-034-fold-signal-detection-binding-and-visibility.md",
        "AgDR-035-fold-apply-marker-first-idempotency.md",
        "AgDR-036-worker-merge-guard-enumerated-deny.md",
        "AgDR-037-review-response-reuses-existing-state-and-edge.md",
        "AgDR-038-in-brief-plain-language-block.md",
        "AgDR-039-per-project-stance-ladder.md",
        "AgDR-040-cross-model-review-by-artifact.md",
        "AgDR-041-runtime-freshness-preflight.md",
        "AgDR-042-orchestrator-singleton-flock.md",
        "AgDR-043-gate-c-owner-is-a-stance-property.md",
        "AgDR-044-status-board-sync-arbitration.md",
        "AgDR-045-gate-states-are-declared-not-inferred.md",
        "AgDR-046-typed-provider-codes-and-latch-notice.md",
        "AgDR-047-fail-review-episode-is-bounded-by-a-durable-marker.md",
        "AgDR-048-single-operator-multi-project.md",
    }
)


def test_decision_record_numbers_are_unique_and_match_headings():
    decisions = Path(__file__).resolve().parents[2] / "self" / ".decisions"
    # Dated first: `AgDR-2026-08-29-slug.md` also satisfies the legacy regex (the
    # year reads as a number), so trying legacy first would mis-parse every new
    # record as `AgDR-2026`.
    dated = re.compile(r"^(ADR|AgDR)-(\d{4}-\d{2}-\d{2})-(.+)\.md$")
    numbered = re.compile(r"^(ADR|AgDR)-(\d+)-.+\.md$")
    # Sweeps are dated, not numbered: they record a re-reading of the decision
    # records rather than a new decision. Predates the #154 changeover and is the
    # form it copied. Heading is pinned differently ("# Sweep <date>").
    sweep = re.compile(r"^SWEEP-(\d{4}-\d{2}-\d{2})-.+\.md$")

    seen_numbers: dict[tuple[str, int], str] = {}
    seen_dated: dict[tuple[str, str, str], str] = {}
    for path in sorted(decisions.glob("*.md")):
        if path.name == "README.md":  # the citation convention, not a record
            continue
        if sw := sweep.match(path.name):
            heading = path.read_text(encoding="utf-8").splitlines()[0]
            assert heading.startswith(f"# Sweep {sw.group(1)}"), (
                f"{path.name}: H1 heading {heading!r} does not carry the "
                f"filename's date {sw.group(1)}"
            )
            continue
        if d := dated.match(path.name):
            # Exact-name duplicates are impossible on a filesystem; the key is
            # casefolded so a slug that differs only in case still trips.
            key = (d.group(1), d.group(2), d.group(3).casefold())
            assert key not in seen_dated, (
                f"duplicate {d.group(1)}-{d.group(2)}-{d.group(3)}: "
                f"{seen_dated[key]} and {path.name}. Two sessions recorded the "
                "same decision on the same day — merge them, or distinguish the "
                "slugs if they are genuinely different decisions."
            )
            seen_dated[key] = path.name

            heading = path.read_text(encoding="utf-8").splitlines()[0]
            assert heading.startswith(f"# {d.group(1)}-{d.group(2)}"), (
                f"{path.name}: H1 heading {heading!r} does not carry the "
                f"filename's date {d.group(1)}-{d.group(2)}"
            )
            continue
        m = numbered.match(path.name)
        assert m, (
            f"{path.name}: does not match (ADR|AgDR)-YYYY-MM-DD-<slug>.md "
            f"(the current form), (ADR|AgDR)-NNN-<slug>.md (legacy, frozen), "
            f"or SWEEP-YYYY-MM-DD-<slug>.md"
        )
        assert path.name in LEGACY_NUMBERED_RECORDS, (
            f"{path.name}: new records are dated, not numbered — name it "
            f"{m.group(1)}-YYYY-MM-DD-<slug>.md (issue #154). Numbering was "
            "dropped because parallel branches allocate the same next-free number."
        )
        key = (m.group(1), int(m.group(2)))
        assert key not in seen_numbers, (
            f"duplicate {m.group(1)}-{m.group(2)}: {seen_numbers[key]} and "
            f"{path.name}."
        )
        seen_numbers[key] = path.name

        heading = path.read_text(encoding="utf-8").splitlines()[0]
        assert heading.startswith(f"# {m.group(1)}-{m.group(2)}"), (
            f"{path.name}: H1 heading {heading!r} does not carry the filename's "
            f"number {m.group(1)}-{m.group(2)} (renumbered file without its heading?)"
        )


def test_legacy_numbered_records_are_not_renamed():
    """The changeover freezes the numbered records; it does not renumber them.

    Their names are cited across prose, code comments, and docstrings, so a
    well-meaning "let's finish the migration" rename would break references
    nothing else checks. Deleting a record is equally out of bounds — the point
    of the corpus is that superseded decisions stay readable.
    """
    decisions = Path(__file__).resolve().parents[2] / "self" / ".decisions"
    present = {path.name for path in decisions.glob("*.md")}
    missing = sorted(LEGACY_NUMBERED_RECORDS - present)
    assert not missing, (
        f"legacy numbered record(s) renamed or deleted: {missing}. The #154 "
        "changeover freezes them as-is — a number means pre-changeover. New "
        "records get a date; old ones keep their number."
    )


def test_workflow_prompts_mint_dated_decision_records():
    """The mint instruction is the surface that actually names records.

    The uniqueness test above is a backstop that fires after a bad name is
    already committed; this line is what stops one being written. If it says
    "next free NNN", workers allocate numbers no matter what the corpus
    convention is — so pin it in every prompt that carries the rule, including
    the codex pilot variant, which the base<->composed sync test does not cover.
    """
    repo_root = Path(__file__).resolve().parents[2]
    for rel in (
        "workflow/WORKFLOW.base.md",
        "projects/switchboard-self/WORKFLOW.md",
        "projects/switchboard-self/WORKFLOW.pilot-codex.md",
    ):
        text = (repo_root / rel).read_text(encoding="utf-8")
        assert ".decisions/AgDR-YYYY-MM-DD-<slug>.md" in text, (
            f"{rel}: worker rule for recording decisions no longer mints the "
            "dated form (issue #154)"
        )
        assert "next free NNN" not in text, (
            f"{rel}: still instructs workers to allocate a sequential number — "
            "that allocator is what #154 removed"
        )


# --- build_credentials() (issue #10: GitHub App identity) ---------------------

def _app_env(tmp_path: Path) -> dict[str, str]:
    pem = tmp_path / "app.pem"
    pem.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n")
    return {
        "SB_APP_ID": "4225392",
        "SB_APP_INSTALLATION_ID": "144657149",
        "SB_APP_PRIVATE_KEY_FILE": str(pem),
        "SB_APP_BOT_LOGIN": "switchboard-agent[bot]",
        "SB_APP_BOT_USER_ID": "300281474",
    }


async def test_build_credentials_static_provider_without_app_env(tmp_path: Path):
    from orchestrator.auth import StaticTokenProvider
    from orchestrator.workflow import build_credentials

    cfg = _cfg_with_tracker(tmp_path)
    async with httpx.AsyncClient() as client:
        creds = build_credentials(cfg.tracker(), {}, client)
        assert isinstance(creds, StaticTokenProvider)
        assert await creds.token() == "literal-token"


async def test_build_credentials_app_provider_with_full_app_env(tmp_path: Path):
    from orchestrator.auth import AppInstallationTokenProvider
    from orchestrator.workflow import build_credentials

    cfg = _cfg_with_tracker(tmp_path)
    async with httpx.AsyncClient() as client:
        creds = build_credentials(cfg.tracker(), _app_env(tmp_path), client)
        assert isinstance(creds, AppInstallationTokenProvider)


def test_build_credentials_partial_app_env_fails_loud(tmp_path: Path):
    """A half-configured App credential set must NOT silently fall back to the
    personal token (silent identity switch); it is a config error."""
    from orchestrator.workflow import build_credentials

    env = _app_env(tmp_path)
    del env["SB_APP_INSTALLATION_ID"]
    cfg = _cfg_with_tracker(tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        build_credentials(cfg.tracker(), env, client=None)
    assert exc_info.value.code == "incomplete_app_credentials"


def test_build_credentials_unreadable_key_file_fails_loud(tmp_path: Path):
    from orchestrator.workflow import build_credentials

    env = _app_env(tmp_path)
    env["SB_APP_PRIVATE_KEY_FILE"] = str(tmp_path / "nope.pem")
    cfg = _cfg_with_tracker(tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        build_credentials(cfg.tracker(), env, client=None)
    assert exc_info.value.code == "unreadable_app_private_key"


def test_validate_dispatch_app_credentials_satisfy_missing_api_key(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """No GITHUB_TOKEN resolved, but a complete SB_APP_* set in the environment
    is a valid credential source (the token is minted at runtime, so api_key
    never resolves)."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    for k, v in _app_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    cfg = _cfg_with_tracker(tmp_path, api_key="$GITHUB_TOKEN")
    validate_dispatch(cfg)  # should not raise


def test_validate_dispatch_partial_app_credentials_fail_loud(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("SB_APP_INSTALLATION_ID", raising=False)
    monkeypatch.delenv("SB_APP_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.setenv("SB_APP_ID", "4225392")
    cfg = _cfg_with_tracker(tmp_path, api_key="$GITHUB_TOKEN")
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(cfg)
    assert exc_info.value.code == "incomplete_app_credentials"


def test_build_credentials_missing_bot_identity_fails_loud(tmp_path: Path):
    """Codex PR #42 P2: App mode with the minting keys but no bot identity
    would mint bot tokens while commits author as whatever git identity the
    workspace inherits — the half-configured identity switch again. The
    completeness check covers all five SB_APP_* keys."""
    from orchestrator.workflow import build_credentials

    env = _app_env(tmp_path)
    del env["SB_APP_BOT_LOGIN"]
    cfg = _cfg_with_tracker(tmp_path)
    with pytest.raises(WorkflowError) as exc_info:
        build_credentials(cfg.tracker(), env, client=None)
    assert exc_info.value.code == "incomplete_app_credentials"
    assert "SB_APP_BOT_LOGIN" in str(exc_info.value)


# --- fold.operator_logins (issue #51 part a) ----------------------------------
#
# The operator allowlist is the ONLY identity surface fold detection has. A
# silently-disabled allowlist is indistinguishable from "the operator hasn't
# reacted yet", so malformed config must fail loudly at startup instead.

def _fold_cfg(tmp_path: Path, block: str) -> Config:
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        f"{block}"
        "---\n"
        "body\n"
    )
    return Config(load_workflow(p), tmp_path)


def test_fold_block_absent_disables_detection(tmp_path: Path):
    assert _fold_cfg(tmp_path, "").fold().operator_logins == ()


def test_fold_empty_list_disables_detection(tmp_path: Path):
    cfg = _fold_cfg(tmp_path, "fold:\n  operator_logins: []\n")
    assert cfg.fold().operator_logins == ()


def test_fold_logins_are_lowercased_and_deduped(tmp_path: Path):
    cfg = _fold_cfg(
        tmp_path,
        "fold:\n  operator_logins: [\"Colin-Prologue\", \" colin-prologue \", \"other\"]\n",
    )
    # GitHub logins are case-insensitive; the stored form is canonical so the
    # detector can compare without re-normalizing at every call site.
    assert cfg.fold().operator_logins == ("colin-prologue", "other")


@pytest.mark.parametrize(
    "block",
    [
        "fold: not-a-map\n",
        "fold:\n  operator_logins: colin\n",
        "fold:\n  operator_logins: [\"\"]\n",
        "fold:\n  operator_logins: [123]\n",
        "fold:\n  operator_lgoins: [\"colin\"]\n",  # typo'd key must not pass silently
    ],
)
def test_fold_malformed_config_raises(tmp_path: Path, block: str):
    with pytest.raises(WorkflowError) as exc_info:
        _fold_cfg(tmp_path, block).fold()
    assert exc_info.value.code == "workflow_parse_error"


def test_validate_dispatch_forces_fold_validation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: $GITHUB_TOKEN\n"
        "fold:\n"
        "  operator_logins: 7\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(Config(load_workflow(p), tmp_path))
    assert exc_info.value.code == "workflow_parse_error"


def _real_base_config(tmp_path: Path, name: str = "WORKFLOW.md") -> Config:
    """The committed `WORKFLOW.base.md` composed for a project that named
    NEITHER an operator nor a review bot — i.e. what registration produces from
    an unset `SB_OPERATOR_LOGIN`/`SB_REVIEW_BOT` (issue #171). Both login
    placeholders collapse to the empty string, so both lists compose to `[]`,
    which is the default the two tests below pin."""
    real_path = Path(__file__).resolve().parents[2] / "workflow" / "WORKFLOW.base.md"
    substituted = (
        real_path.read_text(encoding="utf-8")
        .replace("{{REPO}}", "acme/widgets")
        .replace("{{WORKSPACE_ROOT}}", "/tmp/ws")
        .replace("{{MAX_AGENTS}}", "10")
        .replace("{{CONVENTION_ROOT}}", "")
        .replace("{{OPERATOR_LOGIN_YAML}}", "")
        .replace("{{REVIEW_BOT_YAML}}", "")
    )
    p = tmp_path / name
    p.write_text(substituted)
    return Config(load_workflow(p), tmp_path)


def test_real_workflow_base_declares_an_empty_fold_allowlist(tmp_path: Path):
    """The scaffold ships detection OFF for a project that names no operator:
    an allowlist nobody vetted must never grant fold authority by default. The
    placeholder (issue #171) makes the value settable per project; it does not
    make it default to anything."""
    assert _real_base_config(tmp_path).fold().operator_logins == ()


# --- review_response.bot_logins (issue #43 / AgDR-037) ------------------------
#
# Same posture and same reason as `fold` above: this allowlist is the loop's
# only botness definition, and a typo'd key that silently disabled the responder
# would be indistinguishable from "the bot hasn't reviewed yet". Without this
# accessor the shipped block would load clean and be inert — no top-level
# unknown-key check exists to catch it.

def _rr_cfg(tmp_path: Path, block: str) -> Config:
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        f"{block}"
        "---\n"
        "body\n"
    )
    return Config(load_workflow(p), tmp_path)


def test_review_response_block_absent_disables_the_loop(tmp_path: Path):
    assert _rr_cfg(tmp_path, "").review_response().bot_logins == ()


def test_review_response_empty_list_disables_the_loop(tmp_path: Path):
    cfg = _rr_cfg(tmp_path, "review_response:\n  bot_logins: []\n")
    assert cfg.review_response().bot_logins == ()


def test_review_response_logins_are_lowercased_and_deduped(tmp_path: Path):
    cfg = _rr_cfg(
        tmp_path,
        "review_response:\n"
        "  bot_logins: [\"ChatGPT-Codex-Connector\", \" chatgpt-codex-connector \","
        " \"other-bot\"]\n",
    )
    # The stored form is canonical because it is ALSO what gets written into the
    # round marker's `bots=` field, which a session parses in another process.
    assert cfg.review_response().bot_logins == (
        "chatgpt-codex-connector", "other-bot",
    )


@pytest.mark.parametrize(
    "block",
    [
        "review_response: not-a-map\n",
        "review_response:\n  bot_logins: codex\n",
        "review_response:\n  bot_logins: [\"\"]\n",
        "review_response:\n  bot_logins: [123]\n",
        "review_response:\n  bot_lgoins: [\"codex\"]\n",  # typo'd key: no silent pass
    ],
)
def test_review_response_malformed_config_raises(tmp_path: Path, block: str):
    with pytest.raises(WorkflowError) as exc_info:
        _rr_cfg(tmp_path, block).review_response()
    assert exc_info.value.code == "workflow_parse_error"


def test_validate_dispatch_forces_review_response_validation(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: $GITHUB_TOKEN\n"
        "review_response:\n"
        "  bot_logins: 7\n"
        "---\n"
        "body\n"
    )
    with pytest.raises(WorkflowError) as exc_info:
        validate_dispatch(Config(load_workflow(p), tmp_path))
    assert exc_info.value.code == "workflow_parse_error"


def test_real_workflow_base_ships_the_response_loop_disabled(tmp_path: Path):
    """SHIPPED CONFIG AC: the loop lands DISABLED for a project that named no
    review bot, dead code by design.

    Going live is a deliberate config edit, never a merge side effect — since
    issue #171 that edit is `SB_REVIEW_BOT` in the project's own binding, and an
    unset variable composes to `[]` exactly as the literal did. With no
    `bot_logins` no trigger fires, so no round marker is ever written and the
    prompt addendum stays inert too.
    """
    assert _real_base_config(tmp_path).review_response().bot_logins == ()


def test_composed_self_workflow_enables_both_login_driven_loops(tmp_path: Path):
    """The OTHER carrying file. `projects/switchboard-self/WORKFLOW.md` is the
    composed copy the live instance actually loads, and both loops short-circuit
    on an empty list before any GitHub read — so an empty allowlist here is a
    feature that cannot be switched on, not a feature that is off.

    Supersedes `test_composed_self_workflow_ships_the_response_loop_disabled`
    (issue #43), which asserted `bot_logins == ()` for this same file. That
    assertion was right while the only way to set the field was hand-editing a
    shared template; issue #171 moves the opt-in to this project's tracked
    `project.env`, so merging THAT is the deliberate config edit AgDR-037 asks
    for. See `self/.decisions/AgDR-049-login-config-is-a-project-binding.md`.

    Exact tuples, not "non-empty": a malformed one-element list matches nobody
    and would leave both loops as inert as an empty one.
    """
    real = Path(__file__).resolve().parents[2] / "projects" / "switchboard-self"
    p = tmp_path / "WORKFLOW.md"
    p.write_text((real / "WORKFLOW.md").read_text(encoding="utf-8"))
    cfg = Config(load_workflow(p), tmp_path)
    assert cfg.fold().operator_logins == ("colin-prologue",)
    assert cfg.review_response().bot_logins == ("chatgpt-codex-connector",)


def test_review_response_rejects_logins_that_break_the_marker_grammar(tmp_path: Path):
    """Codex review (PR #134): a whitespace-bearing entry would serialize into
    the round marker's `bots=` field (matched as \\S*), making every marker
    parse as round 0 — the cap never engages and response sessions dispatch
    indefinitely. Malformed config fails the LOAD, never the marker."""
    for bad in ("codex bot", "bots=evil", "a<b", "-lead", "trail-"):
        cfg = _rr_cfg(
            tmp_path, f'review_response:\n  bot_logins: ["{bad}"]\n'
        )
        with pytest.raises(WorkflowError):
            cfg.review_response()
    ok = _rr_cfg(
        tmp_path, 'review_response:\n  bot_logins: ["Codex-Bot[bot]"]\n'
    ).review_response()
    assert ok.bot_logins == ("codex-bot[bot]",)
