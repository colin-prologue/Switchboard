"""Dispatch-guard integration tests (issue #29, part A).

The orchestrator refuses to claim an issue whose current state requires a
provenance marker it lacks — concretely, a `status:todo` without
`gate:triage-passed` (it reached `todo` without passing triage). Refusal is
inert: no claim, no label writes, exactly one guarded comment naming the missing
marker, no repost on later ticks. With the marker present the same issue
dispatches normally.

The fake tracker derives `state` from `status:*` labels via the SAME
`normalize_status_state` helper the real tracker uses (asserted below), so these
tests exercise real state derivation, not hard-coded states.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator.scheduler as scheduler_mod
from orchestrator.scheduler import Orchestrator
from orchestrator.tracker import normalize_status_state
from orchestrator.types import BlockerRef, Issue, TurnResult

UTC = timezone.utc


def make_issue(n: int, labels: list[str], blockers: list[BlockerRef] | None = None) -> Issue:
    """Build an Issue whose state is DERIVED from its labels via the shared
    normalization helper — the fidelity the acceptance criteria require."""
    norm = [l.strip().lower() for l in labels]
    state = normalize_status_state(norm, closed=False)
    return Issue(
        id=f"node-{n}", identifier=str(n), title=f"Issue {n}", description="body",
        priority=None, state=state, branch_name=None,
        url=f"https://github.com/acme/api/issues/{n}", labels=norm,
        blocked_by=blockers or [],
        created_at=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(minutes=n),
        updated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


class FakeTracker:
    def __init__(self):
        self.candidates: list[Issue] = []
        self.states: dict[str, Issue] = {}
        self.comments: list[tuple[str, str]] = []
        self.labels_added: list[tuple[str, tuple[str, ...]]] = []
        self.labels_removed: list[tuple[str, tuple[str, ...]]] = []

    async def fetch_candidate_issues(self):
        return list(self.candidates)

    async def fetch_issues_by_states(self, state_names):
        return []

    async def fetch_issue_states_by_ids(self, ids):
        return [self.states[i] for i in ids if i in self.states]

    async def add_issue_comment(self, issue_id, body):
        self.comments.append((issue_id, body))

    def _issues_with_id(self, issue_id):
        seen = []
        for issue in [*self.candidates, *self.states.values()]:
            if issue.id == issue_id and not any(issue is s for s in seen):
                seen.append(issue)
        return seen

    def _after_label_write(self, issue):
        # Fake fidelity (issue #29 AC): state is DERIVED from labels via the
        # same normalization the real tracker uses, never hard-coded.
        issue.state = normalize_status_state(issue.labels, closed=False)

    async def add_labels(self, issue_id, label_names):
        self.labels_added.append((issue_id, tuple(label_names)))
        for issue in self._issues_with_id(issue_id):
            for name in label_names:
                if name not in issue.labels:
                    issue.labels.append(name)
            self._after_label_write(issue)

    async def remove_labels(self, issue_id, label_names):
        # issue #14 (AgDR-010): the claim swap calls this on every first todo
        # dispatch; mirror of add_labels, recomputing derived state.
        self.labels_removed.append((issue_id, tuple(label_names)))
        drop = set(label_names)
        for issue in self._issues_with_id(issue_id):
            issue.labels = [lbl for lbl in issue.labels if lbl not in drop]
            self._after_label_write(issue)


class FakeRunner:
    provider_id = "fake"

    def __init__(self, hold: bool = False):
        self.hold = hold
        self.release = asyncio.Event()
        self.turns: list[tuple[str, str | None, str]] = []

    async def run_turn(self, workspace, prompt, resume_session_id, on_event,
                       issue_id, agent_token=None):
        self.turns.append((issue_id, resume_session_id, prompt))
        if self.hold:
            await self.release.wait()
        return TurnResult(status="succeeded", session_id=f"sess-{len(self.turns)}",
                          cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 1},
                          num_turns=1)


WORKFLOW_TMPL = """---
tracker:
  kind: github
  repo: "acme/api"
  api_key: "test-token"
  active_states: ["triage", "todo", "in progress"]
  terminal_states: ["closed"]
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


def _build_harness(tmp_path, monkeypatch):
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


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return _build_harness(tmp_path, monkeypatch)


async def wait_for(cond, timeout=3.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while not cond():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


# --- fake fidelity: state derived like the real tracker (not hard-coded) -------

def test_fake_tracker_state_matches_real_normalization():
    cases = [
        (["status:todo"], "todo"),
        (["status:todo", "gate:triage-passed"], "todo"),
        (["status:in-progress"], "in progress"),
        (["bug"], "none"),
    ]
    for labels, expected in cases:
        issue = make_issue(1, labels)
        assert issue.state == normalize_status_state(
            [l.lower() for l in labels], closed=False)
        assert issue.state == expected  # sanity anchor, still derived above


# --- guard behaviour ----------------------------------------------------------

async def test_todo_without_marker_is_refused(harness):
    orch, tracker, runner, _ = harness
    issue = make_issue(1, ["status:todo"])          # no gate:triage-passed
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()

    assert runner.turns == []                        # never dispatched
    assert "node-1" not in orch.running
    assert "node-1" not in orch.claimed
    assert "node-1" not in orch.sessions_per_issue   # no session granted
    assert tracker.labels_added == []                # no label writes
    assert len(tracker.comments) == 1                # exactly one refusal comment
    assert tracker.comments[0][0] == "node-1"
    assert "gate:triage-passed" in tracker.comments[0][1]  # names the missing marker

    # No repost on subsequent ticks.
    await orch._tick()
    await orch._tick()
    assert len(tracker.comments) == 1
    assert tracker.labels_added == []


async def test_todo_with_marker_dispatches_normally(harness):
    orch, tracker, runner, _ = harness
    runner.hold = True                               # hold inside the worker
    issue = make_issue(1, ["status:todo", "gate:triage-passed"])
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()
    await wait_for(lambda: runner.turns)             # worker genuinely in run_turn

    assert len(runner.turns) == 1                    # dispatched
    assert "node-1" in orch.claimed
    assert orch.sessions_per_issue.get("node-1") == 1
    assert tracker.comments == []                    # no refusal comment

    runner.release.set()
    tracker.candidates = []                           # quiesce continuation retry
    tracker.states = {"node-1": make_issue(1, ["status:human-review"])}
    await wait_for(lambda: not orch.running)


async def test_marker_applied_later_clears_refusal_and_dispatches(harness):
    orch, tracker, runner, _ = harness
    runner.hold = True
    issue = make_issue(1, ["status:todo"])
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()
    assert len(tracker.comments) == 1                # refused once
    assert "node-1" not in orch.running

    # Triage retroactively promotes: marker now present -> dispatchable.
    promoted = make_issue(1, ["status:todo", "gate:triage-passed"])
    tracker.candidates = [promoted]
    tracker.states = {"node-1": promoted}
    await orch._tick()
    await wait_for(lambda: runner.turns)             # worker genuinely in run_turn

    assert len(runner.turns) == 1                    # now dispatched
    assert len(tracker.comments) == 1                # refusal comment not duplicated

    runner.release.set()
    tracker.candidates = []
    tracker.states = {"node-1": make_issue(1, ["status:human-review"])}
    await wait_for(lambda: not orch.running)
