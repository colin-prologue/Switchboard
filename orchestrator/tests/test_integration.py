"""Integration tests: the orchestrator loop against fake tracker/runner.

Asserts the spec invariants end-to-end (core §7–§8, §16; owned parking
extension per SPEC.md §4), not just happy paths:
- gated (non-active) states are never dispatched
- blocked todo issues are never dispatched
- global concurrency cap holds under load
- terminal reconciliation cancels the worker and cleans the workspace;
  non-active reconciliation cancels without cleanup
- stall detection terminates and queues a retry
- session-cap exhaustion parks the issue: claim released, ONE comment posted,
  workspace preserved, no re-dispatch until updated_at changes
- restart recovery: startup terminal sweep removes stale workspaces
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import orchestrator.scheduler as scheduler_mod
from orchestrator.scheduler import Orchestrator
from orchestrator.types import BlockerRef, Issue, TurnResult

UTC = timezone.utc


def make_issue(n: int, state: str = "todo", blockers: list[BlockerRef] | None = None,
               updated: str = "2026-07-01T10:00:00+00:00") -> Issue:
    return Issue(
        id=f"node-{n}", identifier=str(n), title=f"Issue {n}",
        description="body", priority=None, state=state, branch_name=None,
        url=f"https://github.com/acme/api/issues/{n}",
        labels=[f"status:{state.replace(' ', '-')}"],
        blocked_by=blockers or [],
        created_at=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(minutes=n),
        updated_at=datetime.fromisoformat(updated),
    )


class FakeTracker:
    def __init__(self):
        self.candidates: list[Issue] = []
        self.states: dict[str, Issue] = {}
        self.terminal: list[Issue] = []
        self.comments: list[tuple[str, str]] = []

    async def fetch_candidate_issues(self):
        return list(self.candidates)

    async def fetch_issues_by_states(self, state_names):
        return list(self.terminal) if state_names else []

    async def fetch_issue_states_by_ids(self, ids):
        return [self.states[i] for i in ids if i in self.states]

    async def add_issue_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        # Mimic GitHub: commenting bumps the issue's updatedAt. The parking
        # marker must survive this (audit finding #1 regression guard).
        bump = datetime.now(UTC)
        for coll in (self.states, ):
            if issue_id in coll:
                coll[issue_id].updated_at = bump
        for issue in self.candidates:
            if issue.id == issue_id:
                issue.updated_at = bump


class FakeRunner:
    """Controllable runner: workers block until released, then succeed."""

    def __init__(self, hold: bool = False):
        self.hold = hold
        self.release = asyncio.Event()
        self.turns: list[tuple[str, str | None]] = []  # (issue_id, resume_sid)

    async def run_turn(self, workspace, prompt, resume_session_id, on_event, issue_id):
        self.turns.append((issue_id, resume_session_id))
        if self.hold:
            await self.release.wait()
        return TurnResult(status="succeeded", session_id="sess-1",
                          cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 1},
                          num_turns=1)


WORKFLOW_TMPL = """---
tracker:
  kind: github
  repo: "acme/api"
  api_key: "test-token"
  active_states: ["todo", "in progress"]
  terminal_states: ["done", "closed", "cancelled"]
polling:
  interval_ms: 100
workspace:
  root: "{ws_root}"
agent:
  max_concurrent_agents: 2
  max_turns: 1
  max_retry_backoff_ms: 500
  max_sessions_per_issue: 2
claude:
  command: "unused-by-fake-runner"
  max_turns: 1
  turn_timeout_ms: 5000
  read_timeout_ms: 3000
  stall_timeout_ms: 0
---
Work {{{{ issue.identifier }}}}: {{{{ issue.title }}}}
"""


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_mod, "CONTINUATION_DELAY_MS", 30)
    monkeypatch.setattr(scheduler_mod, "FAILURE_BASE_BACKOFF_MS", 30)
    ws_root = tmp_path / "ws"
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TMPL.format(ws_root=ws_root))

    orch = Orchestrator(wf)
    orch._load_workflow(initial=True)
    tracker = FakeTracker()
    runner = FakeRunner()
    real_components = orch._components

    def fake_components():
        _, wsm, _ = real_components()
        return tracker, wsm, runner

    orch._components = fake_components
    return orch, tracker, runner, ws_root


async def wait_for(cond, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


async def test_gated_states_never_dispatched(harness):
    orch, tracker, runner, _ = harness
    tracker.candidates = [
        make_issue(1, "drafting"),
        make_issue(2, "plan review"),
        make_issue(3, "human review"),
        make_issue(4, "todo"),
    ]
    tracker.states = {"node-4": make_issue(4, "human review")}  # done after 1 turn
    await orch._tick()
    assert set(orch.running) <= {"node-4"}
    assert orch.sessions_per_issue.get("node-4") == 1
    for gated in ("node-1", "node-2", "node-3"):
        assert gated not in orch.sessions_per_issue
    await wait_for(lambda: not orch.running)


async def test_blocked_todo_never_dispatched(harness):
    orch, tracker, runner, _ = harness
    open_blocker = BlockerRef(id="node-9", identifier="9", state="open")
    closed_blocker = BlockerRef(id="node-8", identifier="8", state="closed")
    tracker.candidates = [
        make_issue(1, "todo", blockers=[open_blocker]),
        make_issue(2, "todo", blockers=[closed_blocker]),
    ]
    tracker.states = {"node-2": make_issue(2, "human review")}
    await orch._tick()
    assert "node-1" not in orch.sessions_per_issue
    assert orch.sessions_per_issue.get("node-2") == 1
    await wait_for(lambda: not orch.running)


async def test_concurrency_cap_holds(harness):
    orch, tracker, runner, _ = harness
    runner.hold = True
    tracker.candidates = [make_issue(n) for n in range(1, 6)]
    tracker.states = {f"node-{n}": make_issue(n, "human review") for n in range(1, 6)}
    await orch._tick()
    assert len(orch.running) == 2  # max_concurrent_agents
    await orch._tick()             # second tick must not exceed the cap
    assert len(orch.running) == 2
    runner.release.set()
    await wait_for(lambda: not orch.running)


async def test_terminal_reconcile_cancels_and_cleans_workspace(harness):
    orch, tracker, runner, ws_root = harness
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    assert "node-1" in orch.running
    wsdir = ws_root / "1"
    await wait_for(lambda: wsdir.is_dir())

    tracker.states = {"node-1": make_issue(1, "closed")}
    await orch._reconcile_running()
    assert "node-1" not in orch.running
    assert "node-1" not in orch.claimed
    assert "node-1" not in orch.retry_attempts
    assert not wsdir.exists()  # terminal -> workspace cleaned (§8.5)


async def test_nonactive_reconcile_cancels_without_cleanup(harness):
    orch, tracker, runner, ws_root = harness
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    wsdir = ws_root / "1"
    await wait_for(lambda: wsdir.is_dir())

    tracker.states = {"node-1": make_issue(1, "plan review")}  # gate, not terminal
    await orch._reconcile_running()
    assert "node-1" not in orch.running
    assert wsdir.is_dir()  # workspace preserved (§8.5 non-active branch)


async def test_stall_detection_terminates_and_retries(harness):
    orch, tracker, runner, _ = harness
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    entry = orch.running["node-1"]
    entry.started_at = datetime.now(UTC) - timedelta(hours=1)  # simulate silence

    # enable stall detection: rewrite the workflow file and force a reload
    wf = orch.workflow_path
    wf.write_text(wf.read_text().replace("stall_timeout_ms: 0",
                                         "stall_timeout_ms: 1000"))
    orch._workflow_mtime = None
    orch._load_workflow(initial=False)

    await orch._reconcile_running()
    assert "node-1" not in orch.running
    assert "node-1" in orch.retry_attempts  # §8.5: stall -> terminate + retry
    assert "node-1" in orch.claimed


async def test_session_cap_parks_issue(harness):
    orch, tracker, runner, ws_root = harness
    issue = make_issue(1)  # stays "todo" forever: agent never moves the label
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()  # session 1: normal exit, issue still active -> continuation
    await wait_for(lambda: orch.sessions_per_issue.get("node-1") == 2)  # session 2
    await wait_for(lambda: "node-1" in orch.parked)  # cap 2 exhausted -> parked

    assert len(tracker.comments) == 1                 # exactly one notification
    assert tracker.comments[0][0] == "node-1"
    assert "parked" in tracker.comments[0][1].lower()
    assert (ws_root / "1").is_dir()                   # workspace preserved
    assert "node-1" not in orch.claimed
    assert "node-1" not in orch.retry_attempts

    await orch._tick()                                # still parked: no re-dispatch
    assert orch.sessions_per_issue.get("node-1", 0) == 2
    assert len(tracker.comments) == 1

    # The parking comment itself bumped updatedAt (FakeTracker mimics GitHub);
    # the issue must STAY parked — the marker is the post-comment value.
    await orch._tick()
    assert "node-1" in orch.parked
    assert orch.sessions_per_issue.get("node-1", 0) == 2
    assert len(tracker.comments) == 1

    # human touches the issue -> updated_at changes -> unparked and dispatchable
    touched = make_issue(1, updated="2026-07-01T12:00:00+00:00")
    tracker.candidates = [touched]
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    assert "node-1" not in orch.parked
    await wait_for(lambda: not orch.running)


async def test_startup_terminal_sweep_removes_stale_workspaces(harness):
    orch, tracker, runner, ws_root = harness
    stale = ws_root / "42"
    stale.mkdir(parents=True)
    tracker.terminal = [make_issue(42, "closed")]
    await orch._startup_terminal_cleanup()
    assert not stale.exists()


async def test_worker_failure_uses_backoff_then_releases_when_gone(harness):
    orch, tracker, runner, _ = harness

    async def failing_turn(workspace, prompt, resume_session_id, on_event, issue_id):
        return TurnResult(status="failed", session_id=None, error="error_during_execution")

    runner.run_turn = failing_turn
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    await wait_for(lambda: "node-1" in orch.retry_attempts)
    assert orch.retry_attempts["node-1"].error is not None

    tracker.candidates = []  # issue disappears -> retry path releases the claim
    await wait_for(lambda: "node-1" not in orch.claimed
                   and "node-1" not in orch.retry_attempts)
