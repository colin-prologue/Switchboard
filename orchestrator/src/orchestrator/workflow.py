"""Workflow file loading and typed configuration views.

implements: core §5 (Workflow Specification), §6 (Configuration Specification)
overridden by: spec/SPEC.md §1 (claude: block replaces the core codex: block),
               spec/SPEC.md §2 (tracker.kind=github, tracker.repo replaces
               project_slug, canonical api_key env is $GITHUB_TOKEN)

Loads WORKFLOW.md-style files (YAML front matter + Markdown prompt body) and
exposes typed, defaulted accessors over the raw config map. Env vars never
globally override YAML; only explicit `$VAR_NAME` values are resolved
(core §6.1).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import httpx
import yaml

from .auth import AppInstallationTokenProvider, StaticTokenProvider
from .types import (
    DEFAULT_WORKSPACE_ROOT,
    AgentConfig,
    ClaudeConfig,
    HooksConfig,
    TrackerConfig,
    WorkflowDefinition,
    WorkflowError,
    resolve_env_indirection,
)


# --- loading (core §5.1/5.2) --------------------------------------------------

def load_workflow(path: Path) -> WorkflowDefinition:
    """Load a WORKFLOW.md-style file into a WorkflowDefinition (core §5.2).

    - File starts with `---`: parse lines up to the next `---` as YAML front
      matter; the remainder is the (trimmed) prompt body.
    - No front matter: the whole file is the prompt body, config is {}.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise WorkflowError("missing_workflow_file", str(e)) from e

    if text.startswith("---"):
        # Split on the delimiter line. The first line is the opening `---`;
        # find the next line that is exactly `---` (front matter fence).
        lines = text.splitlines(keepends=True)
        # lines[0] is the opening fence (possibly with trailing newline).
        end_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx is None:
            # No closing fence: treat everything after the opening fence as
            # front matter with no body (best-effort; still must parse as YAML).
            front_matter_text = "".join(lines[1:])
            body = ""
        else:
            front_matter_text = "".join(lines[1:end_idx])
            body = "".join(lines[end_idx + 1:])

        try:
            raw = yaml.safe_load(front_matter_text)
        except yaml.YAMLError as e:
            raise WorkflowError("workflow_parse_error", str(e)) from e

        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise WorkflowError(
                "workflow_front_matter_not_a_map",
                f"front matter decoded to {type(raw).__name__}, expected a map",
            )

        return WorkflowDefinition(config=raw, prompt_template=body.strip())

    return WorkflowDefinition(config={}, prompt_template=text.strip())


# --- path/value coercion helpers (core §6.1) ---------------------------------

def _expand_path(value: str) -> str:
    """Apply `~` and `$VAR` expansion to a filesystem path value (core §6.1).

    Supports embedded references (e.g. `$HOME/workspaces`), not just
    whole-value `$NAME`. An unresolvable reference is a validation error —
    never a silent empty string (which would quietly relocate the workspace
    root into the workflow directory).
    """
    value = os.path.expandvars(value)
    if "$" in value:
        raise WorkflowError(
            "workflow_parse_error",
            f"unresolved environment reference in path value {value!r}",
        )
    return os.path.expanduser(value)


def _normalize_state_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out = []
    for v in values:
        if isinstance(v, str):
            out.append(v.strip().lower())
    return out


# --- typed config view (core §5.3/§6.4) ---------------------------------------

class Config:
    """Typed, defaulted view over a WorkflowDefinition's raw config map."""

    def __init__(self, defn: WorkflowDefinition, workflow_dir: Path):
        self._config = defn.config
        self._workflow_dir = workflow_dir

    # -- tracker (SPEC.md §2: kind=github, repo=owner/name, $GITHUB_TOKEN) ----

    def tracker(self) -> TrackerConfig:
        raw = self._config.get("tracker")
        raw = raw if isinstance(raw, dict) else {}

        kind = raw.get("kind")
        kind = kind if isinstance(kind, str) else ""

        repo = raw.get("repo")
        repo = repo.strip() if isinstance(repo, str) else ""

        endpoint = raw.get("endpoint")
        endpoint = endpoint if isinstance(endpoint, str) and endpoint else "https://api.github.com/graphql"

        api_key_raw = raw.get("api_key")
        api_key_raw = api_key_raw if isinstance(api_key_raw, str) and api_key_raw else "$GITHUB_TOKEN"
        api_key = resolve_env_indirection(api_key_raw)

        required_labels_raw = raw.get("required_labels", [])
        required_labels = _normalize_state_list(required_labels_raw) if isinstance(required_labels_raw, list) else []

        # Defaults match the SPEC.md §2 binding: triage is an active state
        # (AgDR-006), and issue-closed is the ONLY terminal condition — the
        # core §5.3.1 Linear defaults ("done"/"cancelled") would make a stray
        # status:done label on an OPEN issue terminal, and reconciliation
        # would destroy its in-flight workspace.
        active_states_raw = raw.get("active_states")
        active_states = (
            _normalize_state_list(active_states_raw)
            if isinstance(active_states_raw, list)
            else ["triage", "todo", "in progress"]
        )

        terminal_states_raw = raw.get("terminal_states")
        terminal_states = (
            _normalize_state_list(terminal_states_raw)
            if isinstance(terminal_states_raw, list)
            else ["closed"]
        )

        return TrackerConfig(
            kind=kind,
            repo=repo,
            endpoint=endpoint,
            api_key=api_key,
            required_labels=required_labels,
            active_states=active_states,
            terminal_states=terminal_states,
        )

    # -- polling ---------------------------------------------------------------

    def polling_interval_ms(self) -> int:
        raw = self._config.get("polling")
        raw = raw if isinstance(raw, dict) else {}
        value = raw.get("interval_ms", 30000)
        if not isinstance(value, int) or isinstance(value, bool):
            return 30000
        if value <= 0:
            raise WorkflowError(
                "workflow_parse_error",
                f"polling.interval_ms must be a positive integer, got {value!r}"
                " (a non-positive interval hot-loops the tracker API)",
            )
        return value

    # -- workspace ---------------------------------------------------------------

    def workspace_root(self) -> Path:
        raw = self._config.get("workspace")
        raw = raw if isinstance(raw, dict) else {}
        root_raw = raw.get("root")
        root_raw = root_raw if isinstance(root_raw, str) and root_raw else DEFAULT_WORKSPACE_ROOT

        expanded = _expand_path(root_raw)
        p = Path(expanded)
        if not p.is_absolute():
            p = self._workflow_dir / p
        return Path(os.path.normpath(str(p)))

    # -- hooks ---------------------------------------------------------------

    def hooks(self) -> HooksConfig:
        raw = self._config.get("hooks")
        raw = raw if isinstance(raw, dict) else {}

        def _script(key: str) -> str | None:
            v = raw.get(key)
            return v if isinstance(v, str) and v.strip() else None

        timeout_ms_raw = raw.get("timeout_ms", 60000)
        if isinstance(timeout_ms_raw, bool) or not isinstance(timeout_ms_raw, int) or timeout_ms_raw < 0:
            raise WorkflowError(
                "workflow_parse_error",
                f"hooks.timeout_ms must be a non-negative integer, got {timeout_ms_raw!r}",
            )

        return HooksConfig(
            after_create=_script("after_create"),
            before_run=_script("before_run"),
            after_run=_script("after_run"),
            before_remove=_script("before_remove"),
            timeout_ms=timeout_ms_raw,
        )

    # -- agent ---------------------------------------------------------------

    def agent(self) -> AgentConfig:
        raw = self._config.get("agent")
        raw = raw if isinstance(raw, dict) else {}

        max_concurrent_agents = raw.get("max_concurrent_agents", 10)
        if isinstance(max_concurrent_agents, bool) or not isinstance(max_concurrent_agents, int):
            max_concurrent_agents = 10
        elif max_concurrent_agents <= 0:
            raise WorkflowError(
                "workflow_parse_error",
                f"agent.max_concurrent_agents must be a positive integer, got"
                f" {max_concurrent_agents!r} (0 would poll forever without dispatching)",
            )

        max_turns = raw.get("max_turns", 20)
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
            raise WorkflowError(
                "workflow_parse_error",
                f"agent.max_turns must be a positive integer, got {max_turns!r}",
            )

        max_retry_backoff_ms = raw.get("max_retry_backoff_ms", 300000)
        if isinstance(max_retry_backoff_ms, bool) or not isinstance(max_retry_backoff_ms, int):
            max_retry_backoff_ms = 300000

        by_state_raw = raw.get("max_concurrent_agents_by_state", {})
        by_state: dict[str, int] = {}
        if isinstance(by_state_raw, dict):
            for k, v in by_state_raw.items():
                if not isinstance(k, str):
                    continue
                if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                    continue  # invalid entries silently ignored (core §5.3.5)
                by_state[k.strip().lower()] = v

        max_sessions_per_issue = raw.get("max_sessions_per_issue", 3)
        if isinstance(max_sessions_per_issue, bool) or not isinstance(max_sessions_per_issue, int) or max_sessions_per_issue <= 0:
            max_sessions_per_issue = 3

        return AgentConfig(
            max_concurrent_agents=max_concurrent_agents,
            max_turns=max_turns,
            max_retry_backoff_ms=max_retry_backoff_ms,
            max_concurrent_agents_by_state=by_state,
            max_sessions_per_issue=max_sessions_per_issue,
        )

    # -- claude (SPEC.md §1: pass-through execution block) ----------------------

    def claude(self) -> ClaudeConfig:
        raw = self._config.get("claude")
        raw = raw if isinstance(raw, dict) else {}

        command = raw.get("command")
        command = command if isinstance(command, str) else "claude -p --verbose --output-format stream-json"

        max_turns = raw.get("max_turns", 20)
        if isinstance(max_turns, bool) or not isinstance(max_turns, int) or max_turns <= 0:
            raise WorkflowError(
                "workflow_parse_error",
                f"claude.max_turns must be a positive integer, got {max_turns!r}",
            )

        max_budget_usd_raw = raw.get("max_budget_usd")
        max_budget_usd: float | None
        if max_budget_usd_raw is None:
            max_budget_usd = None
        elif isinstance(max_budget_usd_raw, bool):
            max_budget_usd = None
        elif isinstance(max_budget_usd_raw, (int, float)):
            max_budget_usd = float(max_budget_usd_raw)
        else:
            raise WorkflowError(
                "workflow_parse_error",
                f"claude.max_budget_usd must be numeric, got {max_budget_usd_raw!r}",
            )

        def _timeout(key: str, default: int) -> int:
            v = raw.get(key, default)
            if isinstance(v, bool) or not isinstance(v, int):
                raise WorkflowError(
                    "workflow_parse_error",
                    f"claude.{key} must be an integer, got {v!r}",
                )
            return v

        return ClaudeConfig(
            command=command,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            turn_timeout_ms=_timeout("turn_timeout_ms", 3600000),
            read_timeout_ms=_timeout("read_timeout_ms", 5000),
            stall_timeout_ms=_timeout("stall_timeout_ms", 300000),
        )


# --- credential provider construction (issue #10: GitHub App identity) -------

# All five are required for App mode: the first three mint tokens; the bot
# identity pair drives before_run.sh's git author + credential helper. Codex
# PR #42 P2: accepting the minting keys alone would mint bot tokens while
# commits silently author as whatever git identity the workspace inherits.
_APP_ENV_KEYS = (
    "SB_APP_ID",
    "SB_APP_INSTALLATION_ID",
    "SB_APP_PRIVATE_KEY_FILE",
    "SB_APP_BOT_LOGIN",
    "SB_APP_BOT_USER_ID",
)


def _app_credentials_env(env: Mapping[str, str]) -> dict[str, str] | None:
    """The SB_APP_* credential set from `env`: complete -> dict, absent -> None.

    A PARTIAL set raises — silently falling back to the personal token (or a
    half-configured bot) would be an unnoticed identity switch (agent actions
    attributed to the operator).
    """
    present = {k: env.get(k, "") for k in _APP_ENV_KEYS}
    set_keys = [k for k, v in present.items() if v]
    if not set_keys:
        return None
    if len(set_keys) < len(_APP_ENV_KEYS):
        missing = sorted(set(_APP_ENV_KEYS) - set(set_keys))
        raise WorkflowError(
            "incomplete_app_credentials",
            f"GitHub App credential set is incomplete: missing {', '.join(missing)}"
            f" (set all of {'/'.join(_APP_ENV_KEYS)},"
            " or none to use the GITHUB_TOKEN fallback)",
        )
    return present


def build_credentials(
    tracker: TrackerConfig,
    env: Mapping[str, str],
    client: httpx.AsyncClient | None,
) -> StaticTokenProvider | AppInstallationTokenProvider:
    """Build the process-lifetime token provider (issue #10 / SPEC.md §2).

    Complete SB_APP_* set in `env` -> AppInstallationTokenProvider (the private
    key is read once, here; only the key lives at rest). Otherwise the
    statically-resolved tracker.api_key (dogfood personal-token path).
    """
    app = _app_credentials_env(env)
    if app is None:
        return StaticTokenProvider(tracker.api_key)
    key_file = app["SB_APP_PRIVATE_KEY_FILE"]
    try:
        pem = Path(key_file).read_text(encoding="utf-8")
    except OSError as e:
        raise WorkflowError(
            "unreadable_app_private_key",
            f"cannot read SB_APP_PRIVATE_KEY_FILE {key_file!r}: {e}",
        ) from e
    return AppInstallationTokenProvider(
        app_id=app["SB_APP_ID"],
        private_key_pem=pem,
        installation_id=app["SB_APP_INSTALLATION_ID"],
        client=client,
    )


# --- dispatch preflight validation (core §6.3, adapted per SPEC.md §2) -------

def validate_dispatch(cfg: Config) -> None:
    """Raise WorkflowError if config is unfit for a new dispatch cycle."""
    tracker = cfg.tracker()

    if not tracker.kind or tracker.kind != "github":
        raise WorkflowError(
            "unsupported_tracker_kind",
            f"tracker.kind must be 'github', got {tracker.kind!r}",
        )

    # Credentials come from EITHER a resolved api_key (dogfood personal-token
    # path, `$GITHUB_TOKEN`) OR a complete SB_APP_* set in the environment
    # (issue #10, preferred). App installation tokens are minted at runtime and
    # never resolve into api_key, so check the env. A partial SB_APP_* set
    # raises here even when api_key is present (fail loud, no identity switch).
    if _app_credentials_env(os.environ) is None and not tracker.api_key:
        raise WorkflowError(
            "missing_tracker_api_key",
            "no credentials: set SB_APP_ID/SB_APP_INSTALLATION_ID/"
            "SB_APP_PRIVATE_KEY_FILE (App path) or GITHUB_TOKEN (dogfood)",
        )

    if not tracker.repo or "/" not in tracker.repo or tracker.repo.startswith("/") or tracker.repo.endswith("/"):
        raise WorkflowError(
            "missing_tracker_repo",
            f"tracker.repo must be shaped like owner/name, got {tracker.repo!r}",
        )
    owner, _, name = tracker.repo.partition("/")
    if not owner or not name or "/" in name:
        raise WorkflowError(
            "missing_tracker_repo",
            f"tracker.repo must be shaped like owner/name, got {tracker.repo!r}",
        )

    claude_cfg = cfg.claude()
    if not claude_cfg.command.strip():
        raise WorkflowError("workflow_parse_error", "claude.command must be non-empty")

    # Force typed-getter validation now so invalid agent/hook/workspace/polling
    # values fail startup (§6.3) instead of surfacing as per-tick errors forever.
    cfg.agent()
    cfg.hooks()
    cfg.workspace_root()
    cfg.polling_interval_ms()
