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
    ClaudeOnlyRunnerSelector,
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
        {"claude": {"command": "claude -p --legacy"}},
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
            config={"claude": {"command": "claude -p"}},
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
                "claude": {"command": "claude -p"},
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
    assert issue.id not in orchestrator.sessions_per_issue
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


def test_selection_after_reload_uses_current_config(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        "---\n"
        "tracker:\n"
        "  kind: github\n"
        "  repo: acme/widgets\n"
        "  api_key: test-token\n"
        "claude:\n"
        "  command: claude -p --first\n"
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
