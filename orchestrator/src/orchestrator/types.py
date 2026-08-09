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
from enum import StrEnum
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


@dataclass(frozen=True)
class CommentReaction:
    """One reaction on an issue comment (issue #51 part a).

    `id` is the reaction's GraphQL node id — the per-process dedupe key for a
    reaction-channel fold signal. `content` is GitHub's enum verbatim
    (`THUMBS_UP` / `THUMBS_DOWN` / …); `login` is the reacting user's login,
    read from the `user { login }` field (reactions have no `author`).
    """

    id: str
    content: str
    login: str | None
    created_at: datetime | None = None


@dataclass(frozen=True)
class IssueComment:
    """One top-level issue comment plus its reactions (issue #51 part a).

    GitHub issue comments have NO reply threading — every comment is top-level,
    which is why `/fold` binding needs an explicit rule (see `fold.py`) while a
    reaction is already comment-bound.
    """

    id: str
    body: str
    login: str | None
    created_at: datetime | None = None
    reactions: tuple[CommentReaction, ...] = ()


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


@dataclass(frozen=True)
class FoldConfig:
    """Operator identity for fold-signal detection (issue #51 part a).

    `operator_logins` is an ALLOWLIST of GitHub logins whose 👍/👎 reactions and
    `/fold` // `/no-fold` comments count as fold signals. An empty list disables
    detection entirely (zero API calls) — that is the default, so a project that
    never configures `fold:` pays nothing. Logins are stored lower-cased
    (GitHub logins are case-insensitive).

    The bot identity (`SB_APP_BOT_LOGIN`) is NEVER an operator; that exclusion
    is applied at detection time (`fold.py`) rather than here, so config parsing
    stays free of environment reads.
    """

    operator_logins: tuple[str, ...] = ()


@dataclass
class MixedExecutionConfig:
    """Validated Stage 6 mixed-mode envelope for provider selection."""

    claude: ClaudeConfig
    codex: CodexConfig
    weights: dict[str, int]
    max_concurrent_agents_by_provider: dict[str, int]


DEFAULT_WORKSPACE_ROOT = str(Path(tempfile.gettempdir()) / "symphony_workspaces")


# --- workspaces (core §4.1.4) -------------------------------------------------

@dataclass
class Workspace:
    path: Path                   # absolute per-issue workspace path
    workspace_key: str           # sanitized issue identifier
    created_now: bool


# --- agent runner results/events (core §4.1.6, §10.4; SPEC.md §1) -------------

class FailureClass(StrEnum):
    """Closed Stage 7 taxonomy for provider-scoped failures and refusals."""

    PROVIDER_AUTHENTICATION = "provider_authentication"
    PROVIDER_PLAN_LIMIT = "provider_plan_limit"
    PROVIDER_CREDITS_EXHAUSTED = "provider_credits_exhausted"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_CAPACITY = "provider_capacity"
    PROVIDER_CONTEXT_EXHAUSTED = "provider_context_exhausted"
    ASSIGNMENT_REFUSED = "assignment_refused"
    RUNNER_STARTUP = "runner_startup"
    RUNNER_TIMEOUT = "runner_timeout"
    RUNNER_PROTOCOL = "runner_protocol"
    WORKER_FAILURE = "worker_failure"


class Continuation(StrEnum):
    """How a benign `incomplete` turn continues on the next turn (issue #47).

    Runner-owned, because only the adapter knows whether its session survives
    an early stop. Claude's `--max-turns` exhaustion leaves the conversation
    intact, so the recovery is to resume the same session id. Codex context
    exhaustion has no known headless recovery and therefore stays a failure —
    it deliberately gets no `Continuation` member.
    """

    RESUME_SESSION = "resume_session"   # continue the same session id next turn


@dataclass
class AgentEvent:
    """Structured event emitted upstream to the orchestrator (core §10.4)."""
    event: str                   # session_started | turn_completed | turn_incomplete |
                                 # turn_failed | startup_failed | notification | malformed
    timestamp: datetime
    pid: int | None = None
    usage: dict[str, int] | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class TurnResult:
    """Outcome of one provider-neutral worker turn (SPEC.md §1, issue #47).

    A turn is one adapter invocation (a `claude -p` or `codex exec` run).
    `status` is the neutral outcome shared by both runners:

    - "succeeded"  — the turn completed its work.
    - "incomplete" — the turn ended early and BENIGNLY: the provider stopped
      at a resource ceiling, not because the work failed. Work is unfinished
      and continues on the next turn per `continuation`. This is NOT a failure
      and must not inflate failure/retry accounting.
    - "failed"     — the turn failed; see `failure_class`.
    - "timed_out"  — the turn exceeded its timeout (a failure).

    `continuation` is populated only for "incomplete" and is runner-owned,
    because only the adapter knows whether its session survives the early stop.
    """
    status: str                  # "succeeded" | "incomplete" | "failed" | "timed_out"
    session_id: str | None       # provider session id (thread identity; reuse via --resume)
    error: str | None = None     # normalized category per core §10.6 when failed
    failure_class: FailureClass | None = None
    continuation: Continuation | None = None  # set only when status == "incomplete"
    cost_usd: float = 0.0
    usage: dict[str, int] = field(default_factory=dict)
    num_turns: int = 0           # provider-internal turn count for the invocation


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
