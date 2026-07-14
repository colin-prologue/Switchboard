"""Core domain model.

implements: core §4 (Core Domain Model)
overridden by: spec/SPEC.md §1 (claude block replaces codex block),
               spec/SPEC.md §2 (tracker.repo replaces project_slug; states are
               status:* labels; identifier is the issue number)

This module is the shared contract between all orchestrator modules. Keep it
dependency-free (stdlib only).
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


# --- issues (core §4.1.1) ----------------------------------------------------

@dataclass
class BlockerRef:
    id: str | None
    identifier: str | None
    state: str | None  # normalized lowercase; "closed" is terminal


@dataclass
class Issue:
    id: str                      # GraphQL node id (stable tracker-internal ID)
    identifier: str              # issue number as string (workspace naming, logs)
    title: str
    description: str | None
    priority: int | None         # GitHub has no priority -> always None (sorts last)
    state: str                   # normalized: status:<x> label with "-" -> " "; closed issue -> "closed"
    branch_name: str | None
    url: str | None
    labels: list[str] = field(default_factory=list)        # lowercased
    blocked_by: list[BlockerRef] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


# --- workflow definition (core §4.1.2) ---------------------------------------

@dataclass
class WorkflowDefinition:
    config: dict[str, Any]       # YAML front matter root object
    prompt_template: str         # trimmed Markdown body


# --- typed config views (core §5.3/§6.4; claude block per SPEC.md §1) --------

@dataclass
class TrackerConfig:
    kind: str                    # "github"
    repo: str                    # owner/name (REQUIRED when kind == "github")
    endpoint: str                # default https://api.github.com/graphql
    api_key: str                 # resolved after $VAR indirection ("" if unresolved)
    required_labels: list[str]
    active_states: list[str]     # normalized lowercase
    terminal_states: list[str]   # normalized lowercase


@dataclass
class HooksConfig:
    after_create: str | None
    before_run: str | None
    after_run: str | None
    before_remove: str | None
    timeout_ms: int              # default 60000


@dataclass
class AgentConfig:
    max_concurrent_agents: int           # default 10
    max_turns: int                       # default 20
    max_retry_backoff_ms: int            # default 300000
    max_concurrent_agents_by_state: dict[str, int]
    # Owned extension (SPEC.md §4, "caps as diagnostic checkpoints"): total
    # worker sessions allowed per issue per process lifetime before the issue
    # is parked (claim released, one notification comment, no re-dispatch).
    # Always on: invalid or non-positive values coerce back to the default —
    # the cap cannot be disabled (parking is the diagnostic checkpoint).
    max_sessions_per_issue: int          # default 3


@dataclass
class ClaudeConfig:
    """Pass-through execution block per SPEC.md §1 (replaces core codex block)."""
    command: str                 # default "claude -p --verbose --output-format stream-json"
    max_turns: int               # per-invocation --max-turns
    max_budget_usd: float | None # per-run cost ceiling (--max-budget-usd)
    turn_timeout_ms: int         # default 3600000
    read_timeout_ms: int         # default 5000 (time to first protocol line)
    stall_timeout_ms: int        # default 300000; <= 0 disables stall detection


@dataclass
class CodexConfig:
    """Standalone Codex CLI adapter settings; not workflow-selectable yet."""

    command: str = (
        "codex --ask-for-approval never --sandbox workspace-write "
        "--config sandbox_workspace_write.network_access=true"
    )
    turn_timeout_ms: int = 3600000
    read_timeout_ms: int = 30000
    stall_timeout_ms: int = 300000


DEFAULT_WORKSPACE_ROOT = str(Path(tempfile.gettempdir()) / "symphony_workspaces")


# --- workspaces (core §4.1.4) -------------------------------------------------

@dataclass
class Workspace:
    path: Path                   # absolute per-issue workspace path
    workspace_key: str           # sanitized issue identifier
    created_now: bool


# --- agent runner results/events (core §4.1.6, §10.4; SPEC.md §1) -------------

@dataclass
class AgentEvent:
    """Structured event emitted upstream to the orchestrator (core §10.4)."""
    event: str                   # session_started | turn_completed | turn_failed |
                                 # startup_failed | notification | malformed
    timestamp: datetime
    pid: int | None = None
    usage: dict[str, int] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    """Outcome of one `claude -p` invocation (one logical turn, SPEC.md §1)."""
    status: str                  # "succeeded" | "failed" | "timed_out"
    session_id: str | None       # claude session id (thread identity; reuse via --resume)
    error: str | None = None     # normalized category per core §10.6 when failed
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    num_turns: int = 0           # claude-internal turn count for the invocation


EventCallback = Callable[[str, AgentEvent], None]  # (issue_id, event) -> None


# --- retry queue (core §4.1.7) -------------------------------------------------

@dataclass
class RetryEntry:
    issue_id: str
    identifier: str
    attempt: int                 # 1-based
    timer_handle: Any            # asyncio.TimerHandle or Task


# --- errors --------------------------------------------------------------------

class WorkflowError(Exception):
    """Typed workflow/config errors (core §5.5). `code` is the error class."""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class TrackerError(Exception):
    """Tracker adapter errors (core §11.4). `code` is the error category."""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class HookError(Exception):
    """Workspace hook failure/timeout (core §9.4)."""


class WorkspaceError(Exception):
    """Workspace creation/containment failures (core §9.5)."""


def resolve_env_indirection(value: str) -> str:
    """Resolve `$VAR_NAME` config indirection (core §6.1).

    Only applies when the whole value is a `$NAME` reference. An unset or
    empty variable resolves to "" (treated as missing by validation).
    """
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return os.environ.get(value[1:], "")
    return value


def sanitize_workspace_key(identifier: str) -> str:
    """Core §4.2 / §9.5 invariant 3: only [A-Za-z0-9._-], others -> '_'.

    Explicit ASCII class — str.isalnum() would admit all Unicode letters.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", identifier)
