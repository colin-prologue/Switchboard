"""Tests for workflow loading and typed config views.

implements: core §17.1 (Workflow and Config Parsing test matrix), adapted for
the GitHub/Claude bindings per SPEC.md §1-2.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.types import WorkflowDefinition, WorkflowError
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
    defn = WorkflowDefinition(config={"claude": {"command": cmd}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.claude().command == cmd


def test_claude_max_budget_usd_float(tmp_path: Path):
    defn = WorkflowDefinition(config={"claude": {"max_budget_usd": 5}}, prompt_template="")
    cfg = Config(defn, tmp_path)
    assert cfg.claude().max_budget_usd == 5.0
    assert isinstance(cfg.claude().max_budget_usd, float)


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
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
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
            "claude": {"command": "   "},
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
