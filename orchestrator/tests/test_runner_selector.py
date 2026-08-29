"""Stage 3 tests for explicit, injectable agent-runner selection."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.codex_runner import CodexRunner
from orchestrator.runner import ClaudeRunner
from orchestrator.runner_selector import (
    AssignmentRefused,
    ClaudeOnlyRunnerSelector,
    CodexGuardUnavailable,
    CodexOnlyRunnerSelector,
    MixedAssignmentRefused,
    MixedRunnerSelector,
)
from orchestrator.scheduler import DispatchResult, Orchestrator
from orchestrator.types import (
    FailureClass,
    Issue,
    RetryEntry,
    TrackerError,
    WorkflowDefinition,
)
from orchestrator.workflow import Config


def _issue() -> Issue:
    return Issue(
        id="node-69",
        identifier="69",
        title="Inject the scheduler runner selector",
        description="body",
        priority=None,
        state="todo",
        branch_name=None,
        url="https://github.com/acme/widgets/issues/69",
        labels=["status:todo", "gate:triage-passed"],
        created_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
        updated_at=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    "execution_config",
    [
        # AgDR-2026-08-29-retire-the-legacy-claude-block left one execution shape, so the parametrization that used to
        # pair legacy-vs-envelope now pairs a bare envelope against a fully
        # specified one.
        {"providers": {"claude": {"kind": "claude-cli"}}},
        {
            "providers": {
                "claude": {
                    "kind": "claude-cli",
                    "command": "claude -p --provider-envelope",
                }
            }
        },
    ],
)
def test_default_selector_constructs_only_claude(
    tmp_path: Path,
    execution_config: dict,
) -> None:
    cfg = Config(
        WorkflowDefinition(config=execution_config, prompt_template="prompt"),
        tmp_path,
    )

    selector = ClaudeOnlyRunnerSelector()
    runner = selector.select(cfg, _issue())

    assert selector.provider_id == "claude"
    assert isinstance(runner, ClaudeRunner)
    assert runner.provider_id == "claude"
    assert runner.cfg == cfg.claude()


def test_codex_only_selector_constructs_only_codex(tmp_path: Path) -> None:
    cfg = Config(
        WorkflowDefinition(
            config={
                "providers": {
                    "codex": {
                        "kind": "codex-cli",
                        "command": "codex --sandbox workspace-write",
                    }
                }
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )

    selector = CodexOnlyRunnerSelector()
    runner = selector.select(cfg, _issue())

    assert selector.provider_id == "codex"
    assert isinstance(runner, CodexRunner)
    assert runner.provider_id == "codex"
    assert runner.cfg == cfg.codex()


class _FakeRunner:
    provider_id = "fake"


class _RecordingSelector:
    provider_id = "claude"

    def __init__(self, runner: _FakeRunner) -> None:
        self.runner = runner
        self.calls: list[tuple[Config, Issue]] = []

    def select(self, cfg: Config, issue: Issue) -> _FakeRunner:
        self.calls.append((cfg, issue))
        return self.runner


class _FailingSelector:
    provider_id = "claude"

    def select(self, cfg: Config, issue: Issue) -> _FakeRunner:
        raise RuntimeError("selector unavailable")


def test_orchestrator_uses_injected_selector(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    runner = _FakeRunner()
    selector = _RecordingSelector(runner)
    orchestrator = Orchestrator(workflow_path, runner_selector=selector)
    cfg = Config(
        WorkflowDefinition(
            config={"providers": {"claude": {"kind": "claude-cli", "command": "claude -p"}}},
            prompt_template="prompt",
        ),
        tmp_path,
    )
    orchestrator._cfg = cfg
    issue = _issue()

    selected = orchestrator._select_runner(issue)

    assert selected is runner
    assert selector.calls == [(cfg, issue)]


async def test_selector_failure_does_not_claim_or_relabel_issue(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=_FailingSelector(),
    )
    orchestrator._cfg = Config(
        WorkflowDefinition(
            config={
                "agent": {"max_sessions_per_issue": 3},
                "providers": {"claude": {"kind": "claude-cli", "command": "claude -p"}},
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )
    issue = _issue()

    with pytest.raises(RuntimeError, match="selector unavailable"):
        await orchestrator._dispatch(issue, attempt=None)

    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert issue.labels == ["status:todo", "gate:triage-passed"]
    assert issue.state == "todo"


def _mixed_config(
    tmp_path: Path,
    *,
    weights: dict[str, int] | None = None,
    global_cap: int = 10,
    provider_caps: dict[str, int] | None = None,
) -> Config:
    return Config(
        WorkflowDefinition(
            config={
                "agent": {
                    "max_concurrent_agents": global_cap,
                    "max_sessions_per_issue": 3,
                    **(
                        {"max_concurrent_agents_by_provider": provider_caps}
                        if provider_caps is not None
                        else {}
                    ),
                },
                "providers": {
                    "claude": {"kind": "claude-cli", "command": "claude -p"},
                    "codex": {"kind": "codex-cli", "command": "codex"},
                },
                "routing": {"weights": weights or {"claude": 1, "codex": 1}},
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )


def _write_mixed_workflow(path: Path, *, claude_weight: int, codex_weight: int) -> None:
    path.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: literal-token\n"
        "providers:\n"
        "  claude:\n"
        "    kind: claude-cli\n"
        "  codex:\n"
        "    kind: codex-cli\n"
        "routing:\n"
        "  weights:\n"
        f"    claude: {claude_weight}\n"
        f"    codex: {codex_weight}\n"
        "---\n"
        "prompt\n"
    )


def test_mixed_selector_prefers_durable_assignment_over_operator_label(
    tmp_path: Path,
) -> None:
    issue = _issue()
    issue.labels.extend(["provider:codex", "agent:claude"])

    first = MixedRunnerSelector().select(_mixed_config(tmp_path), issue)
    second = MixedRunnerSelector().select(_mixed_config(tmp_path), issue)

    assert isinstance(first, CodexRunner)
    assert first.provider_id == "codex"
    assert second.provider_id == "codex"


def test_mixed_selector_uses_stable_sha256_weight_bucket(tmp_path: Path) -> None:
    issue = _issue()
    weights = {"claude": 3, "codex": 2}
    expected_bucket = int.from_bytes(
        hashlib.sha256(issue.id.encode("utf-8")).digest(), "big"
    ) % sum(weights.values())
    expected_provider = "claude" if expected_bucket < weights["claude"] else "codex"

    first = MixedRunnerSelector().select(_mixed_config(tmp_path, weights=weights), issue)
    second = MixedRunnerSelector().select(_mixed_config(tmp_path, weights=weights), issue)

    assert first.provider_id == expected_provider
    assert second.provider_id == expected_provider


@pytest.mark.parametrize(
    "labels",
    [
        ["provider:claude", "provider:codex"],
        ["agent:claude", "agent:codex"],
        ["provider:unsupported"],
        ["agent:unsupported"],
    ],
)
def test_mixed_selector_refuses_conflicting_or_unknown_labels(
    tmp_path: Path,
    labels: list[str],
) -> None:
    issue = _issue()
    issue.labels.extend(labels)

    with pytest.raises(MixedAssignmentRefused):
        MixedRunnerSelector().select(_mixed_config(tmp_path), issue)


async def test_mixed_dispatch_logs_assignment_refusal(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(tmp_path)
    issue = _issue()
    issue.labels.extend(["provider:claude", "provider:codex"])

    await orchestrator._dispatch(issue, attempt=None)

    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    err = capfd.readouterr().err
    assert "outcome=refused" in err
    assert "failure_class=assignment_refused" in err


class _LabelTracker:
    def __init__(self, add_error: TrackerError | None = None) -> None:
        self.add_error = add_error
        self.operations: list[tuple[str, tuple[str, ...]]] = []
        self.candidates: list[Issue] = []

    async def fetch_candidate_issues(self) -> list[Issue]:
        return list(self.candidates)

    async def fetch_open_issues(self) -> list[Issue]:
        # issue #52: the tick fetches unfiltered, then filters locally.
        return await self.fetch_candidate_issues()

    def select_candidates(self, issues: list[Issue]) -> list[Issue]:
        return list(issues)

    async def add_labels(self, issue_id: str, labels: list[str]) -> None:
        del issue_id
        if self.add_error is not None:
            raise self.add_error
        self.operations.append(("add", tuple(labels)))

    async def remove_labels(self, issue_id: str, labels: list[str]) -> None:
        del issue_id
        self.operations.append(("remove", tuple(labels)))


class _BlockingAssignmentTracker(_LabelTracker):
    def __init__(self) -> None:
        super().__init__()
        self.assignment_started = asyncio.Event()
        self.release_assignment = asyncio.Event()

    async def add_labels(self, issue_id: str, labels: list[str]) -> None:
        await super().add_labels(issue_id, labels)
        if labels[0].startswith("provider:"):
            self.assignment_started.set()
            await self.release_assignment.wait()


class _MixedRecordingSelector(MixedRunnerSelector):
    def __init__(self) -> None:
        self.selected_providers: list[str] = []

    def select(self, cfg: Config, issue: Issue) -> _FakeRunner:
        provider_id = self.select_provider(cfg.mixed().weights, issue)
        self.selected_providers.append(provider_id)
        runner = _FakeRunner()
        runner.provider_id = provider_id
        runner.turn_timeout_ms = 1000
        runner.stall_timeout_ms = 0
        runner.max_budget_usd = None
        return runner


async def test_mixed_dispatch_persists_assignment_before_status_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(tmp_path, weights={"claude": 100, "codex": 0})
    issue = _issue()
    issue.labels.append("agent:codex")
    tracker = _LabelTracker()
    blocker = asyncio.Event()

    async def _hold_worker(*args, **kwargs) -> None:
        await blocker.wait()

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _hold_worker)

    await orchestrator._dispatch(issue, attempt=None)

    assert tracker.operations == [
        ("add", ("provider:codex",)),
        ("add", ("status:in-progress",)),
        ("remove", ("status:todo",)),
    ]
    assert orchestrator.running[issue.id].provider_id == "codex"
    assert "provider:codex" in issue.labels

    orchestrator.running[issue.id].task.cancel()
    await asyncio.gather(orchestrator.running[issue.id].task, return_exceptions=True)


async def test_mixed_assignment_write_failure_leaves_issue_unclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(tmp_path)
    issue = _issue()
    tracker = _LabelTracker(TrackerError("github_api_status", "write failed"))
    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))

    await orchestrator._dispatch(issue, attempt=None)

    assert tracker.operations == []
    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert "provider:claude" not in issue.labels
    assert "provider:codex" not in issue.labels


async def test_open_circuit_refuses_before_new_mixed_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(
        tmp_path,
        weights={"claude": 0, "codex": 100},
    )
    orchestrator._provider_circuit("codex").record_failure(
        FailureClass.PROVIDER_CREDITS_EXHAUSTED)
    issue = _issue()
    tracker = _LabelTracker()

    async def _unexpected_worker(*args, **kwargs) -> None:
        pytest.fail("an open provider circuit must not launch a worker")

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _unexpected_worker)

    outcome = await orchestrator._dispatch(issue, attempt=None)

    assert outcome.result is DispatchResult.CIRCUIT_BLOCKED
    assert outcome.provider_id == "codex"
    assert tracker.operations == []
    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert orchestrator.sessions_for_issue(issue.id) == {}
    assert "provider:codex" not in issue.labels


async def test_mixed_assignment_write_reserves_issue_before_awaiting_tracker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(tmp_path)
    issue = _issue()
    tracker = _BlockingAssignmentTracker()
    blocker = asyncio.Event()

    async def _hold_worker(*args, **kwargs) -> None:
        await blocker.wait()

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _hold_worker)
    dispatch = asyncio.create_task(orchestrator._dispatch(issue, attempt=1))

    await tracker.assignment_started.wait()

    assert issue.id in orchestrator.claimed
    assert not orchestrator._should_dispatch(issue)

    tracker.release_assignment.set()
    await dispatch
    orchestrator.running[issue.id].task.cancel()
    await asyncio.gather(orchestrator.running[issue.id].task, return_exceptions=True)


async def test_mixed_dispatch_reuses_existing_assignment_without_a_second_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(tmp_path, weights={"claude": 100, "codex": 0})
    issue = _issue()
    issue.labels.append("provider:codex")
    tracker = _LabelTracker()
    blocker = asyncio.Event()

    async def _hold_worker(*args, **kwargs) -> None:
        await blocker.wait()

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _hold_worker)

    await orchestrator._dispatch(issue, attempt=None)

    assert tracker.operations == [
        ("add", ("status:in-progress",)),
        ("remove", ("status:todo",)),
    ]
    assert orchestrator.running[issue.id].provider_id == "codex"

    orchestrator.running[issue.id].task.cancel()
    await asyncio.gather(orchestrator.running[issue.id].task, return_exceptions=True)


async def test_mixed_dispatch_persists_full_provider_assignment_without_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(
        tmp_path,
        global_cap=2,
        provider_caps={"codex": 1},
    )
    orchestrator.running["already-codex"] = SimpleNamespace(provider_id="codex")
    issue = _issue()
    issue.labels.append("agent:codex")
    tracker = _LabelTracker()

    async def _unexpected_worker(*args, **kwargs) -> None:
        pytest.fail("a full provider must not launch a worker")

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _unexpected_worker)

    await orchestrator._dispatch(issue, attempt=None)

    assert tracker.operations == [("add", ("provider:codex",))]
    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert "provider:codex" in issue.labels
    assert orchestrator._provider_slots_available("claude")
    err = capfd.readouterr().err
    assert "provider_id=codex" in err
    assert "outcome=refused" in err
    assert "failure_class=provider_capacity" in err


def test_mixed_provider_capacity_defaults_to_global_cap(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(
        workflow_path,
        runner_selector=MixedRunnerSelector(),
    )
    orchestrator._cfg = _mixed_config(tmp_path, global_cap=1)
    orchestrator.running["already-codex"] = SimpleNamespace(provider_id="codex")

    assert not orchestrator._provider_slots_available("codex")
    assert orchestrator._provider_slots_available("claude")


async def test_mixed_retry_uses_durable_assignment_after_weights_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    selector = _MixedRecordingSelector()
    orchestrator = Orchestrator(workflow_path, runner_selector=selector)
    orchestrator._cfg = _mixed_config(tmp_path, weights={"claude": 100, "codex": 0})
    issue = _issue()
    issue.state = "in progress"
    issue.labels = ["status:in-progress", "provider:codex"]
    tracker = _LabelTracker()
    tracker.candidates = [issue]
    blocker = asyncio.Event()

    async def _hold_worker(*args, **kwargs) -> None:
        await blocker.wait()

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _hold_worker)
    orchestrator.retry_attempts[issue.id] = RetryEntry(
        issue_id=issue.id,
        identifier=issue.identifier,
        attempt=1,
        timer_handle=SimpleNamespace(cancel=lambda: None),
    )

    await orchestrator._on_retry_timer(issue.id)

    assert selector.selected_providers == ["codex"]
    assert orchestrator.running[issue.id].provider_id == "codex"
    assert tracker.operations == []

    orchestrator.running[issue.id].task.cancel()
    await asyncio.gather(orchestrator.running[issue.id].task, return_exceptions=True)


def test_mixed_hot_reload_keeps_durable_provider_assignment(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_mixed_workflow(workflow_path, claude_weight=0, codex_weight=100)
    orchestrator = Orchestrator(workflow_path, runner_selector=MixedRunnerSelector())
    orchestrator._load_workflow(initial=True)
    issue = _issue()
    issue.labels.append("provider:codex")

    _write_mixed_workflow(workflow_path, claude_weight=100, codex_weight=0)
    stat = workflow_path.stat()
    os.utime(workflow_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    orchestrator._load_workflow(initial=False)

    runner = orchestrator._select_runner(issue)

    assert isinstance(runner, CodexRunner)


def test_mixed_restart_uses_durable_provider_assignment(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    _write_mixed_workflow(workflow_path, claude_weight=0, codex_weight=100)
    first = Orchestrator(workflow_path, runner_selector=MixedRunnerSelector())
    first._load_workflow(initial=True)
    issue = _issue()
    issue.labels.append("provider:codex")

    _write_mixed_workflow(workflow_path, claude_weight=100, codex_weight=0)
    restarted = Orchestrator(workflow_path, runner_selector=MixedRunnerSelector())
    restarted._load_workflow(initial=True)

    runner = restarted._select_runner(issue)

    assert isinstance(runner, CodexRunner)


# --- Codex has no guard surface (issue #135, residual of #133 / AgDR-036) ----
#
# The Claude adapter injects `guard.py` via `--settings` every turn; the Codex
# adapter injects nothing, so a Codex session denies none of the enumerated
# Gate-C shapes. Until that changes, the selector refuses to route Codex to a
# project whose stance hands Gate C to an agent — the one configuration where
# the guard is doing more than restating the prompt, because there it BOUNDS an
# otherwise-real merge right to the granting project's own repository.

AGENT_GATE_REPO = "acme/widgets"


def _agent_gate_tracker() -> dict:
    """A `prototype`-shaped tracker: the handoff state IS dispatched, so an
    agent owns Gate C (`TrackerConfig.agent_owns_gate_c`)."""
    return {
        "kind": "github",
        "repo": AGENT_GATE_REPO,
        "api_key": "literal-token",
        "active_states": ["todo", "in progress", "review"],
        "handoff_label": "status:review",
    }


def _agent_gate_config(tmp_path: Path) -> Config:
    cfg = _mixed_config(tmp_path)
    cfg._config["tracker"] = _agent_gate_tracker()
    return cfg


def test_codex_only_selector_refuses_a_project_whose_agent_owns_gate_c(
    tmp_path: Path,
) -> None:
    cfg = Config(
        WorkflowDefinition(
            config={
                "tracker": _agent_gate_tracker(),
                "providers": {"codex": {"kind": "codex-cli", "command": "codex"}},
            },
            prompt_template="prompt",
        ),
        tmp_path,
    )

    with pytest.raises(CodexGuardUnavailable) as excinfo:
        CodexOnlyRunnerSelector().select(cfg, _issue())

    # the diagnostic has to name the project, or an operator reading one log
    # line cannot tell which board stopped moving
    assert AGENT_GATE_REPO in str(excinfo.value)


@pytest.mark.parametrize("labels", [["provider:codex"], ["agent:codex"]])
def test_mixed_selector_refuses_codex_on_an_agent_owned_gate(
    tmp_path: Path,
    labels: list[str],
) -> None:
    """Label precedence is the live route to Codex regardless of weights
    (`codex: 0` today), so both the durable assignment and the operator label
    have to hit the refusal."""
    issue = _issue()
    issue.labels.extend(labels)

    with pytest.raises(CodexGuardUnavailable):
        MixedRunnerSelector().select(_agent_gate_config(tmp_path), issue)


def test_mixed_selector_refuses_codex_by_weight_on_an_agent_owned_gate(
    tmp_path: Path,
) -> None:
    """The unlabelled hash-bucket path is refused too — a project cannot reach
    Codex by routing weights either."""
    cfg = _agent_gate_config(tmp_path)
    cfg._config["routing"] = {"weights": {"claude": 0, "codex": 100}}

    with pytest.raises(CodexGuardUnavailable):
        MixedRunnerSelector().select(cfg, _issue())


def test_the_refusal_is_provider_scoped_not_a_project_wide_block(
    tmp_path: Path,
) -> None:
    """Claude keeps running the same agent-gated project, and keeps being told
    the repo that bounds its merge right. A refusal that stopped the whole
    board would be a regression dressed as a guard."""
    issue = _issue()
    issue.labels.append("provider:claude")

    runner = MixedRunnerSelector().select(_agent_gate_config(tmp_path), issue)

    assert isinstance(runner, ClaudeRunner)
    assert runner.gate_c_repo == AGENT_GATE_REPO


def test_codex_still_runs_where_a_human_owns_gate_c(tmp_path: Path) -> None:
    """The refusal is conditioned on the stance, not on the provider: a
    human-gated project (every project shipped today) still routes to Codex."""
    issue = _issue()
    issue.labels.append("provider:codex")
    cfg = _mixed_config(tmp_path)
    cfg._config["tracker"] = {
        "kind": "github",
        "repo": AGENT_GATE_REPO,
        "api_key": "literal-token",
        "active_states": ["triage", "todo", "in progress"],
        "handoff_label": "status:human-review",
    }

    assert isinstance(MixedRunnerSelector().select(cfg, issue), CodexRunner)


async def test_codex_refusal_leaves_the_issue_untouched_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """End of the dispatch path: no claim, no provider label, no status swap,
    no worker — the same handling ambiguous provider labels already get."""
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("prompt")
    orchestrator = Orchestrator(workflow_path, runner_selector=MixedRunnerSelector())
    orchestrator._cfg = _agent_gate_config(tmp_path)
    issue = _issue()
    issue.labels.append("provider:codex")
    tracker = _LabelTracker()

    async def _unexpected_worker(*args, **kwargs) -> None:
        pytest.fail("an unguarded provider must not launch a worker")

    monkeypatch.setattr(orchestrator, "_components", lambda: (tracker, None))
    monkeypatch.setattr(orchestrator, "_worker", _unexpected_worker)

    outcome = await orchestrator._dispatch(issue, attempt=None)

    assert outcome.result is DispatchResult.REFUSED
    assert tracker.operations == []
    assert issue.id not in orchestrator.claimed
    assert issue.id not in orchestrator.running
    assert issue.labels == ["status:todo", "gate:triage-passed", "provider:codex"]
    assert issue.state == "todo"
    err = capfd.readouterr().err
    assert "outcome=refused" in err
    assert "failure_class=assignment_refused" in err


def test_the_scheduler_catches_the_base_not_one_subclass() -> None:
    """Both refusals must reach the same handler. Catching
    `MixedAssignmentRefused` would let a Codex refusal escape as an unhandled
    exception and take the dispatch loop with it."""
    assert issubclass(MixedAssignmentRefused, AssignmentRefused)
    assert issubclass(CodexGuardUnavailable, AssignmentRefused)


def test_selection_after_reload_uses_current_config(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: test-token\n"
        "providers:\n"
        "  claude:\n"
        "    kind: claude-cli\n"
        "    command: claude -p --first\n"
        "---\n"
        "prompt\n"
    )
    selector = _RecordingSelector(_FakeRunner())
    orchestrator = Orchestrator(workflow_path, runner_selector=selector)
    orchestrator._load_workflow(initial=True)
    issue = _issue()

    orchestrator._select_runner(issue)
    first_cfg = selector.calls[-1][0]

    workflow_path.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: test-token\n"
        "providers:\n"
        "  claude:\n"
        "    kind: claude-cli\n"
        "    command: claude -p --second\n"
        "---\n"
        "prompt\n"
    )
    stat = workflow_path.stat()
    os.utime(workflow_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    orchestrator._load_workflow(initial=False)

    orchestrator._select_runner(issue)
    second_cfg = selector.calls[-1][0]

    assert first_cfg.claude().command == "claude -p --first"
    assert second_cfg.claude().command == "claude -p --second"
    assert second_cfg is orchestrator._cfg
