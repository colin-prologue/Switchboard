"""Integration tests: the orchestrator loop against fake tracker/runner.

Asserts the spec invariants end-to-end (core §7–§8, §16; owned parking
extension per SPEC.md §4), not just happy paths:
- gated (non-active) states are never dispatched
- blocked todo issues are never dispatched
- global concurrency cap holds under load
- terminal reconciliation cancels the worker and cleans the workspace;
  non-active reconciliation cancels without cleanup
- stall detection terminates and queues a retry
- an active -> active state change ends the session at the turn boundary
  (role-pinned sessions, SPEC.md §4 — the triage PASS handoff)
- session-cap exhaustion parks the issue: claim released, ONE comment posted,
  workspace preserved, no re-dispatch until updated_at changes
- restart recovery: startup terminal sweep removes stale workspaces
- a wedged after_run hook cannot freeze the poll loop: _terminate hands the
  worker await to a background teardown task that reports back (retry/claim/
  cleanup) only after the worker fully exits; the claim stays held meanwhile
- shutdown is bounded by SHUTDOWN_TEARDOWN_GRACE_MS even with a wedged hook
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import orchestrator.scheduler as scheduler_mod
from orchestrator.provider_circuit import CircuitState, ProviderCircuit
from orchestrator.scheduler import (
    CLAIM_RELEASE_COMMENT,
    CONTINUATION_PROMPT,
    IN_PROGRESS_LABEL,
    TODO_LABEL,
    Orchestrator,
    ProviderWaitEntry,
)
from orchestrator.types import (
    BlockerRef,
    Continuation,
    FailureClass,
    Issue,
    TrackerError,
    TurnResult,
)
from orchestrator.workspace import WorkspaceManager

UTC = timezone.utc


def make_issue(n: int, state: str = "todo", blockers: list[BlockerRef] | None = None,
               updated: str = "2026-07-01T10:00:00+00:00") -> Issue:
    # A dispatchable todo has passed triage, so it carries gate:triage-passed
    # (issue #29): the orchestrator dispatch guard refuses a status:todo without
    # it. Other states don't require the marker.
    labels = [f"status:{state.replace(' ', '-')}"]
    if state == "todo":
        labels.append("gate:triage-passed")
    return Issue(
        id=f"node-{n}", identifier=str(n), title=f"Issue {n}",
        description="body", priority=None, state=state, branch_name=None,
        url=f"https://github.com/acme/api/issues/{n}",
        labels=labels,
        blocked_by=blockers or [],
        created_at=datetime(2026, 6, 1, tzinfo=UTC) + timedelta(minutes=n),
        updated_at=datetime.fromisoformat(updated),
    )


def _recompute_state_from_labels(issue: Issue) -> None:
    """Mirror tracker._normalize_issue's status:* -> state derivation.

    Conformance note (OBS-023): the FakeTracker must recompute issue.state from
    its status:* labels on EVERY label write, exactly as the real
    `_normalize_issue` does on read. Without this, the orchestrator's own
    status-label writes would be invisible to the worker's between-turn state
    refresh, and the role-pin regression that AgDR-010 decision #3 guards
    against (a todo-dispatch label write burning a session) would be invisible
    to every integration test. A closed issue keeps state "closed" (labels are
    only meaningful while open), matching the real normalizer.
    """
    if issue.state == "closed":
        return
    status_labels = sorted(lbl for lbl in issue.labels if lbl.startswith("status:"))
    if status_labels:
        issue.state = status_labels[0][len("status:"):].replace("-", " ")
    else:
        issue.state = "none"


class FakeTracker:
    """Conformance note (OBS-023): this fake models three things the real
    tracker's read/write contract implies and that issue #14 depends on —
    (1) label REMOVAL (`remove_labels`, mirroring the real GraphQL mutation),
    (2) the label-write -> updatedAt echo (a write bumps updated_at), and
    (3) recomputing issue.state from status:* labels on every label write
    (see `_recompute_state_from_labels`). All three are required for the
    role-pin and revert behaviours to be observable end-to-end.
    """

    def __init__(self):
        self.candidates: list[Issue] = []
        self.states: dict[str, Issue] = {}
        self.terminal: list[Issue] = []
        self.comments: list[tuple[str, str]] = []
        self.labels_added: list[tuple[str, tuple[str, ...]]] = []
        self.labels_removed: list[tuple[str, tuple[str, ...]]] = []
        self.open_prs: dict[str, list[dict]] = {}
        self.sole_status_swaps: list[tuple[str, str]] = []
        self.add_labels_error: TrackerError | None = None  # set to simulate a write failure
        self.remove_labels_error: TrackerError | None = None

    async def fetch_candidate_issues(self):
        return list(self.candidates)

    async def fetch_issues_by_states(self, state_names):
        return list(self.terminal) if state_names else []

    async def fetch_issue_states_by_ids(self, ids):
        return [self.states[i] for i in ids if i in self.states]

    async def add_issue_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        # Mimic GitHub: commenting bumps the issue's updatedAt. Parking no longer
        # keys off updatedAt (the label is authoritative), but the bump is real,
        # so the fake keeps modelling it — a stray bump must NOT unpark.
        bump = datetime.now(UTC)
        if issue_id in self.states:
            self.states[issue_id].updated_at = bump
        for issue in self.candidates:
            if issue.id == issue_id:
                issue.updated_at = bump

    async def add_labels(self, issue_id, label_names):
        if self.add_labels_error is not None:
            raise self.add_labels_error
        # Mimic GitHub: the label becomes visible on every subsequent fetch of
        # the issue. This is the durable state that survives a "restart" — a test
        # that rebuilds the scheduler but reuses the tracker still sees the label.
        self.labels_added.append((issue_id, tuple(label_names)))
        for issue in self._issues_with_id(issue_id):
            for name in label_names:
                if name not in issue.labels:
                    issue.labels.append(name)
            self._after_label_write(issue)

    async def remove_labels(self, issue_id, label_names):
        if self.remove_labels_error is not None:
            raise self.remove_labels_error
        # Mirror of add_labels: removeLabelsFromLabelable makes the label vanish
        # from every subsequent fetch. Recomputes state + bumps updatedAt too.
        self.labels_removed.append((issue_id, tuple(label_names)))
        drop = set(label_names)
        for issue in self._issues_with_id(issue_id):
            issue.labels = [lbl for lbl in issue.labels if lbl not in drop]
            self._after_label_write(issue)

    async def fetch_open_prs(self, head_ref):
        # issue #61 handoff surface: [{"number", "head_sha", "closes"}].
        return [dict(pr) for pr in self.open_prs.get(head_ref, [])]

    async def set_sole_status_label(
            self, issue_id, label,
            expected_status=("status:in-progress", "status:todo")):
        # Mirror of the real tracker method: preemption guard, then add +
        # remove-other-status inside one call, then a read-back verification
        # that derives state from labels the same way (fake fidelity).
        issues = self._issues_with_id(issue_id)
        if not issues:
            raise TrackerError("handoff_label_verify_failed", "issue not found")
        fetched = issues[0]
        if fetched.state.lower() == "closed":
            raise TrackerError("handoff_preempted", "issue closed")
        current_status = sorted(
            l for l in fetched.labels if l.startswith("status:"))
        if len(current_status) != 1 or current_status[0] not in expected_status:
            raise TrackerError(
                "handoff_preempted",
                f"{current_status} not in {sorted(expected_status)}")
        current = list(fetched.labels)
        added_now = label not in current
        if added_now:
            await self.add_labels(issue_id, [label])
        stale = [l for l in current if l.startswith("status:") and l != label]
        if stale:
            try:
                await self.remove_labels(issue_id, stale)
            except TrackerError:
                # Mirror of the real tracker's compensating rollback.
                if added_now:
                    self.remove_labels_error = None
                    await self.remove_labels(issue_id, [label])
                raise
        after = sorted(
            l for l in self._issues_with_id(issue_id)[0].labels
            if l.startswith("status:")
        )
        if after != [label]:
            raise TrackerError(
                "handoff_label_verify_failed", f"{after} != [{label!r}]"
            )
        self.sole_status_swaps.append((issue_id, label))

    def _issues_with_id(self, issue_id):
        # candidates and states may hold DISTINCT Issue objects for one id (the
        # two fetch paths); a real label write is visible on both, so apply to all.
        return [i for i in (*self.candidates, *self.states.values())
                if i.id == issue_id]

    @staticmethod
    def _after_label_write(issue: Issue) -> None:
        # (2) updatedAt echo and (3) state recompute — see the class docstring.
        _recompute_state_from_labels(issue)
        issue.updated_at = datetime.now(UTC)


class FakeRunner:
    """Controllable runner: workers block until released, then succeed.

    Returns a distinct session id per turn (sess-1, sess-2, ...) so tests can
    assert the scheduler resumes with the LATEST session id, not a stale one.
    """

    provider_id = "fake"

    def __init__(self, hold: bool = False):
        self.turn_timeout_ms = 5000
        self.stall_timeout_ms = 0
        self.max_budget_usd: float | None = None
        self.hold = hold
        self.release = asyncio.Event()
        # (issue_id, resume_sid, prompt)
        self.turns: list[tuple[str, str | None, str]] = []
        self.tokens: list[str | None] = []  # agent_token per turn (issue #10)

    async def run_turn(self, workspace, prompt, resume_session_id, on_event,
                       issue_id, agent_token=None):
        self.turns.append((issue_id, resume_session_id, prompt))
        self.tokens.append(agent_token)
        if self.hold:
            await self.release.wait()
        return TurnResult(status="succeeded", session_id=f"sess-{len(self.turns)}",
                          cost_usd=0.01, usage={"input_tokens": 1, "output_tokens": 1},
                          num_turns=1)


class ScriptedRunner:
    """Runner whose per-turn TurnResult is produced by a `factory(n, resume)`
    callback (n is the 1-based turn index). Optionally blocks on a given turn
    (`hold_on_turn`) until `release` is set, so tests can inspect the
    scheduler's mid-continuation state (issue #47)."""

    provider_id = "fake"

    def __init__(self, factory, hold_on_turn: int | None = None):
        self.turn_timeout_ms = 5000
        self.stall_timeout_ms = 0
        self.max_budget_usd: float | None = None
        self.factory = factory
        self.hold_on_turn = hold_on_turn
        self.release = asyncio.Event()
        self.turns: list[tuple[str, str | None, str]] = []
        self.tokens: list[str | None] = []

    async def run_turn(self, workspace, prompt, resume_session_id, on_event,
                       issue_id, agent_token=None):
        n = len(self.turns) + 1
        self.turns.append((issue_id, resume_session_id, prompt))
        self.tokens.append(agent_token)
        if self.hold_on_turn == n:
            await self.release.wait()
        return self.factory(n, resume_session_id)


class FixedRunnerSelector:
    def __init__(self, runner, provider_id="claude"):
        self.runner = runner
        self.provider_id = provider_id

    def select(self, cfg, issue):
        provider_cfg = (
            cfg.codex() if self.provider_id == "codex" else cfg.claude()
        )
        self.runner.turn_timeout_ms = provider_cfg.turn_timeout_ms
        self.runner.stall_timeout_ms = provider_cfg.stall_timeout_ms
        self.runner.max_budget_usd = getattr(
            provider_cfg,
            "max_budget_usd",
            None,
        )
        return self.runner


class MixedFixedRunnerSelector:
    provider_id = "mixed"

    def __init__(self, runners):
        self.runners = runners

    def select(self, cfg, issue):
        provider_id = next(
            label.removeprefix("provider:")
            for label in issue.labels
            if label.startswith("provider:")
        )
        runner = self.runners[provider_id]
        provider_cfg = getattr(cfg.mixed(), provider_id)
        runner.turn_timeout_ms = provider_cfg.turn_timeout_ms
        runner.stall_timeout_ms = provider_cfg.stall_timeout_ms
        runner.max_budget_usd = getattr(provider_cfg, "max_budget_usd", None)
        return runner


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


CODEX_WORKFLOW_TMPL = WORKFLOW_TMPL.replace(
    """claude:
  command: "unused-by-fake-runner"
  max_turns: 1
  turn_timeout_ms: 5000
  read_timeout_ms: 3000
  stall_timeout_ms: 0""",
    """providers:
  codex:
    kind: codex-cli
    command: "unused-by-fake-runner"
    turn_timeout_ms: 7000
    read_timeout_ms: 3000
    stall_timeout_ms: 0""",
)


MIXED_WORKFLOW_TMPL = WORKFLOW_TMPL.replace(
    "max_concurrent_agents: 2",
    """max_concurrent_agents: 4
  max_concurrent_agents_by_provider:
    claude: 2
    codex: 2""",
).replace(
    """claude:
  command: "unused-by-fake-runner"
  max_turns: 1
  turn_timeout_ms: 5000
  read_timeout_ms: 3000
  stall_timeout_ms: 0""",
    """providers:
  claude:
    kind: claude-cli
    command: "unused-by-fake-runner"
    max_turns: 1
    turn_timeout_ms: 5000
    read_timeout_ms: 3000
    stall_timeout_ms: 0
  codex:
    kind: codex-cli
    command: "unused-by-fake-runner"
    turn_timeout_ms: 5000
    read_timeout_ms: 3000
    stall_timeout_ms: 0
routing:
  weights:
    claude: 100
    codex: 0""",
)


def _build_harness(
    tmp_path,
    monkeypatch,
    workflow_tmpl=WORKFLOW_TMPL,
    runner=None,
    provider_id="claude",
    runner_selector=None,
):
    monkeypatch.setattr(scheduler_mod, "CONTINUATION_DELAY_MS", 30)
    monkeypatch.setattr(scheduler_mod, "FAILURE_BASE_BACKOFF_MS", 30)
    ws_root = tmp_path / "ws"
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(workflow_tmpl.format(ws_root=ws_root))

    runner = runner if runner is not None else FakeRunner()
    orch = Orchestrator(
        wf,
        runner_selector=(
            runner_selector
            if runner_selector is not None
            else FixedRunnerSelector(runner, provider_id=provider_id)
        ),
    )
    orch._load_workflow(initial=True)
    tracker = FakeTracker()
    real_components = orch._components

    def fake_components():
        _, wsm = real_components()
        return tracker, wsm

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
    assert "node-1" not in orch.running   # authority taken immediately
    # teardown (worker await + cleanup) reports back asynchronously
    await wait_for(lambda: not wsdir.exists())  # terminal -> cleaned (§8.5)
    await wait_for(lambda: "node-1" not in orch.claimed)
    assert "node-1" not in orch.retry_attempts


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


async def test_stall_detection_terminates_and_retries(harness, monkeypatch, capfd):
    orch, tracker, runner, _ = harness
    # keep the retry entry observable (capped at 500ms) once teardown lands
    monkeypatch.setattr(scheduler_mod, "FAILURE_BASE_BACKOFF_MS", 10000)
    # Execution policy is pinned when the runner is selected for dispatch.
    wf = orch.workflow_path
    wf.write_text(wf.read_text().replace("stall_timeout_ms: 0",
                                         "stall_timeout_ms: 1000"))
    orch._workflow_mtime = None
    orch._load_workflow(initial=False)
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    entry = orch.running["node-1"]
    entry.started_at = datetime.now(UTC) - timedelta(hours=1)  # simulate silence

    await orch._reconcile_running()
    assert "node-1" not in orch.running
    assert "node-1" in orch.claimed
    # §8.5: stall -> terminate + retry, scheduled once teardown reports back
    await wait_for(lambda: "node-1" in orch.retry_attempts)
    err = capfd.readouterr().err
    assert "provider_id=fake" in err
    assert "outcome=failed" in err
    assert "failure_class=runner_timeout" in err


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
    # issue #14: the todo dispatch made the claim visible (status:in-progress),
    # then park clears it and adds the durable status:parked marker — the
    # one-status-label contract holds across the transition.
    assert tracker.labels_added == [
        ("node-1", ("status:in-progress",)),          # todo dispatch: claim visible
        ("node-1", ("status:parked",)),               # durable park marker
    ]
    assert ("node-1", ("status:todo",)) in tracker.labels_removed         # dispatch swap
    assert ("node-1", ("status:in-progress",)) in tracker.labels_removed  # cleared at park
    assert "status:parked" in issue.labels            # visible on future fetches
    assert "status:in-progress" not in issue.labels   # one-status-label contract
    assert (ws_root / "1").is_dir()                   # workspace preserved
    assert "node-1" not in orch.claimed
    assert "node-1" not in orch.retry_attempts

    await orch._tick()                                # still parked: no re-dispatch
    assert orch.sessions_per_issue.get("node-1", 0) == 2
    assert len(tracker.comments) == 1
    assert len(tracker.labels_added) == 2             # not re-labelled past park

    # The parking comment bumped updatedAt (FakeTracker mimics GitHub); the
    # label — not updatedAt — is authoritative, so the issue STAYS parked.
    await orch._tick()
    assert "node-1" in orch.parked
    assert orch.sessions_per_issue.get("node-1", 0) == 2
    assert len(tracker.comments) == 1

    # human removes the status:parked label -> unparked, counter reset, dispatchable
    unparked = make_issue(1)  # labels back to just ["status:todo"]
    tracker.candidates = [unparked]
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    assert "node-1" not in orch.parked
    # counter reset on unpark: the re-dispatch is a FRESH session 1, not a
    # continuation of the pre-park count (which would immediately re-park).
    assert orch.sessions_per_issue.get("node-1") == 1
    await wait_for(lambda: not orch.running)
    await wait_for(lambda: not orch.running)


async def test_parked_issue_not_redispatched_after_restart(tmp_path, monkeypatch):
    """Restart-amnesia guard (AgDR-002 weakest point → resolved).

    A prior process parked the issue by writing the durable ``status:parked``
    label. THIS scheduler instance is fresh: empty ``parked`` set, zero session
    counter. It must not re-dispatch the issue — the tracker label, not
    in-memory state, is the source of truth. Before this fix a restart re-granted
    the full cap to every parked issue.
    """
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch)
    parked = make_issue(1, "todo")
    parked.labels = ["status:todo", "status:parked"]  # label survived the restart
    tracker.candidates = [parked]
    tracker.states = {"node-1": parked}

    await orch._tick()
    await orch._tick()

    assert runner.turns == []                            # never dispatched
    assert "node-1" not in orch.running
    assert "node-1" not in orch.claimed
    assert orch.sessions_per_issue.get("node-1", 0) == 0  # no fresh cap granted
    assert tracker.comments == []                        # no duplicate park comment


async def test_park_label_write_failure_holds_at_cap_without_looping(harness):
    """Codex PR #28 P1: if the durable label write fails, `_park` must not leave
    the issue in a state that unparks itself on the next tick.

    Before the fix, `_park` added the issue to `self.parked` *before* the label
    write; when the write failed the next `_eligible` saw "in parked + no label",
    took the unpark branch (resetting the counter), and re-dispatched — an
    unbounded cap→park→fail→unpark spend loop. The counter must stay at cap and
    the comment must be posted exactly once.
    """
    orch, tracker, runner, _ = harness
    tracker.add_labels_error = TrackerError("github_api_status", "transient boom")
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()
    await wait_for(lambda: orch.sessions_per_issue.get("node-1") == 2)  # ran to cap
    for _ in range(4):                                # keep ticking; write keeps failing
        await orch._tick()
        await asyncio.sleep(0.02)

    assert orch.sessions_per_issue.get("node-1") == 2  # counter held at cap, NOT reset
    assert len(runner.turns) == 2                      # no bonus sessions past the cap
    assert len(tracker.comments) == 1                  # notified once, no spam
    assert "node-1" not in orch.parked                 # not durably parked (label absent)

    # Recovery: once the write succeeds, the next park attempt makes it durable.
    tracker.add_labels_error = None
    await orch._tick()
    await wait_for(lambda: "node-1" in orch.parked)
    assert ("node-1", ("status:parked",)) in tracker.labels_added
    assert len(tracker.comments) == 1                  # still only one comment total


async def test_park_missing_label_halts_dispatch(harness):
    """Codex PR #28 P1 (the cited case): if `status:parked` is not provisioned,
    the durable park marker can never be written, so the cap cannot be enforced
    across restarts. Rather than silently re-grant caps, halt dispatch loudly."""
    orch, tracker, runner, _ = harness
    tracker.add_labels_error = TrackerError("github_label_not_found", "not provisioned")
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()
    await wait_for(lambda: orch._park_label_missing is not None)  # park tripped the halt

    # A brand-new dispatchable issue must NOT be picked up while dispatch is halted.
    tracker.candidates = [issue, make_issue(2)]
    tracker.states["node-2"] = make_issue(2, "human review")
    await orch._tick()
    assert "node-2" not in orch.running
    assert "node-2" not in orch.sessions_per_issue


async def test_active_to_active_state_change_ends_session(tmp_path, monkeypatch):
    """Role-pin override (SPEC.md §4): a triage PASS relabel (triage -> todo,
    both active) ends the session at the turn boundary instead of feeding
    continuation prompts to the stale verifier role until max_turns."""
    tmpl = (WORKFLOW_TMPL
            .replace('active_states: ["todo", "in progress"]',
                     'active_states: ["triage", "todo", "in progress"]')
            .replace("max_turns: 1", "max_turns: 3"))
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)

    tracker.candidates = [make_issue(1, "triage")]
    tracker.states = {"node-1": make_issue(1, "todo")}  # PASS routed during turn 1

    await orch._tick()
    tracker.candidates = []  # quiesce: continuation retry finds no candidate
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)
    assert len(runner.turns) == 1      # no continuation turns after the relabel
    assert runner.turns[0][1] is None  # and that turn was a fresh session


async def test_next_turn_token_refresh_remains_reconcilable(tmp_path, monkeypatch):
    """A prior success cannot shield a worker preparing its next provider turn."""
    tmpl = WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 2")
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    token_calls = 0
    token_started = asyncio.Event()
    token_release = asyncio.Event()

    async def delayed_second_token(_turn_timeout_ms):
        nonlocal token_calls
        token_calls += 1
        if token_calls == 2:
            token_started.set()
            await token_release.wait()
        return "test-token"

    monkeypatch.setattr(orch, "_agent_token", delayed_second_token)
    issue = make_issue(1, "in progress")
    tracker.candidates = [issue]
    tracker.states = {issue.id: issue}

    await orch._tick()
    await wait_for(token_started.is_set)
    assert not orch.running[issue.id].turn_succeeded

    tracker.states = {issue.id: make_issue(1, "human review")}
    await orch._reconcile_running()

    assert issue.id not in orch.running
    token_release.set()
    await wait_for(lambda: issue.id not in orch.claimed)


def _wedged_after_run(monkeypatch):
    """Patch WorkspaceManager.run_after_run with a hook that blocks until
    released, standing in for a wedged after_run script (which the real
    _run_hook would only abandon at hooks.timeout_ms — 120s in production)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def wedged(self, ws):
        started.set()
        await release.wait()

    monkeypatch.setattr(WorkspaceManager, "run_after_run", wedged)
    return started, release


async def test_stall_terminate_with_wedged_after_run_does_not_block_tick(
        harness, monkeypatch):
    """Regression: _terminate awaited the cancelled worker inline, so the
    after_run hook in its `finally` froze the poll loop for up to
    hooks.timeout_ms per stalled worker. Termination must return immediately;
    retry is scheduled only after the worker fully exits, and the claim is
    held throughout so the issue cannot be re-dispatched into a workspace
    whose after_run is still running."""
    orch, tracker, runner, _ = harness
    # keep the retry entry observable once it appears (capped at 500ms by
    # max_retry_backoff_ms) instead of the harness's 30ms
    monkeypatch.setattr(scheduler_mod, "FAILURE_BASE_BACKOFF_MS", 10000)
    hook_started, hook_release = _wedged_after_run(monkeypatch)
    wf = orch.workflow_path
    wf.write_text(wf.read_text().replace("stall_timeout_ms: 0",
                                         "stall_timeout_ms: 1000"))
    orch._workflow_mtime = None
    orch._load_workflow(initial=False)
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    await wait_for(lambda: runner.turns)    # worker genuinely inside run_turn
    orch.running["node-1"].started_at = datetime.now(UTC) - timedelta(hours=1)

    loop = asyncio.get_event_loop()
    t0 = loop.time()
    # bounded so a regression fails instead of hanging the suite
    await asyncio.wait_for(orch._reconcile_running(), timeout=5.0)
    assert loop.time() - t0 < 0.5           # the tick is not held hostage
    assert "node-1" not in orch.running     # authority taken immediately
    await wait_for(hook_started.is_set)     # worker is wedged in after_run

    # teardown in flight: claim held, retry not yet scheduled
    assert "node-1" in orch.claimed
    assert "node-1" not in orch.retry_attempts
    await orch._tick()                      # a full tick also completes...
    assert "node-1" not in orch.running     # ...without re-dispatching

    tracker.candidates = []                 # quiesce the eventual retry
    hook_release.set()
    await wait_for(lambda: "node-1" in orch.retry_attempts)  # reported back


async def test_terminal_cleanup_waits_for_wedged_after_run(harness, monkeypatch):
    """Terminal reconciliation must not rmtree the workspace while the
    worker's after_run hook is still running in it — cleanup happens in the
    background teardown task after the worker exits."""
    orch, tracker, runner, ws_root = harness
    hook_started, hook_release = _wedged_after_run(monkeypatch)
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    wsdir = ws_root / "1"
    await wait_for(lambda: wsdir.is_dir())

    tracker.states = {"node-1": make_issue(1, "closed")}
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    # bounded so a regression fails instead of hanging the suite
    await asyncio.wait_for(orch._reconcile_running(), timeout=5.0)
    assert loop.time() - t0 < 0.5
    assert "node-1" not in orch.running
    await wait_for(hook_started.is_set)
    assert wsdir.is_dir()                   # cleanup must not race the hook

    hook_release.set()
    await wait_for(lambda: not wsdir.exists())
    await wait_for(lambda: "node-1" not in orch.claimed)
    assert "node-1" not in orch.retry_attempts


async def test_teardown_cleanup_uses_original_root_across_reload(harness, monkeypatch):
    """Terminal cleanup must target the workspace the worker actually used,
    even if the workflow hot-reloads workspace.root during the (long) teardown
    window. Regression for PR #25 review: _finish_termination must not resolve
    the WorkspaceManager from post-reload config after the await."""
    orch, tracker, runner, ws_root = harness
    hook_started, hook_release = _wedged_after_run(monkeypatch)
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    old_wsdir = ws_root / "1"
    await wait_for(lambda: old_wsdir.is_dir())

    tracker.states = {"node-1": make_issue(1, "closed")}
    await asyncio.wait_for(orch._reconcile_running(), timeout=5.0)
    await wait_for(hook_started.is_set)  # teardown parked on the wedged hook

    # operator moves workspace.root mid-teardown; a tick reloads the config
    new_root = ws_root.parent / "ws2"
    wf = orch.workflow_path
    wf.write_text(wf.read_text().replace(f'root: "{ws_root}"',
                                         f'root: "{new_root}"'))
    orch._workflow_mtime = None
    orch._load_workflow(initial=False)
    assert orch._cfg.workspace_root() == new_root  # reload took effect

    hook_release.set()
    await wait_for(lambda: not old_wsdir.exists())  # ORIGINAL workspace cleaned
    assert not new_root.exists()                    # new root never touched
    await wait_for(lambda: "node-1" not in orch.claimed)


async def test_shutdown_bounded_despite_wedged_after_run(harness, monkeypatch):
    """SIGTERM shutdown drains workers (whose `finally` runs after_run) for at
    most the teardown grace, then hard-cancels the stragglers."""
    orch, tracker, runner, _ = harness
    monkeypatch.setattr(scheduler_mod, "SHUTDOWN_TEARDOWN_GRACE_MS", 200,
                        raising=False)
    _hook_started, _never_released = _wedged_after_run(monkeypatch)
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    assert "node-1" in orch.running
    await wait_for(lambda: runner.turns)    # worker genuinely inside run_turn

    await asyncio.wait_for(orch.shutdown(), timeout=2.0)  # not 120s
    await wait_for(lambda: not orch.running)


async def test_startup_terminal_sweep_removes_stale_workspaces(harness):
    orch, tracker, runner, ws_root = harness
    stale = ws_root / "42"
    stale.mkdir(parents=True)
    tracker.terminal = [make_issue(42, "closed")]
    await orch._startup_terminal_cleanup()
    assert not stale.exists()


async def test_multi_turn_continuation_resumes_session(tmp_path, monkeypatch, capfd):
    """Turn 2+ inside ONE worker session must resume the previous turn's
    session id and send CONTINUATION_PROMPT, never the rendered task prompt
    (core §16.5, §7.1). A regression that drops the session id between turns
    (turns[n][1] becomes None) or resumes a stale id must fail here."""
    tmpl = WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 3")
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)

    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1, "todo")}  # state never changes

    await orch._tick()
    tracker.candidates = []  # quiesce: post-session continuation retry releases
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)

    assert len(runner.turns) == 3  # ran to agent.max_turns in one session
    # turn 1: fresh session, rendered task prompt
    assert runner.turns[0][1] is None
    assert runner.turns[0][2] == "Work 1: Issue 1"
    # turn 2 resumes turn 1's session; turn 3 resumes turn 2's (latest wins)
    assert runner.turns[1][1] == "sess-1"
    assert runner.turns[2][1] == "sess-2"
    for _, _, prompt in runner.turns[1:]:
        assert prompt == CONTINUATION_PROMPT
    # Normal exit, not a failure (the write-only `completed` set was removed
    # in the v0.1.4 audit — assert the observable outcome instead).
    err = capfd.readouterr().err
    assert "worker completed" in err
    assert "worker failed" not in err
    assert "provider_id=fake" in err
    assert "outcome=started" in err
    assert "outcome=completed" in err


async def test_budget_ceiling_ends_session_normally(tmp_path, monkeypatch, capfd):
    """claude.max_budget_usd caps the CUMULATIVE session cost: at $0.01/turn a
    $0.025 ceiling ends the session after turn 3 (0.03 >= 0.025) as a normal
    completion, well before agent.max_turns (§13.5 accounting)."""
    tmpl = (WORKFLOW_TMPL
            .replace("max_turns: 1", "max_turns: 10")
            .replace('command: "unused-by-fake-runner"',
                     'command: "unused-by-fake-runner"\n  max_budget_usd: 0.025'))
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)

    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1, "todo")}  # state never changes

    await orch._tick()
    tracker.candidates = []
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)

    assert len(runner.turns) == 3          # ceiling, not max_turns (10), ended it
    # Normal completion, not WorkerFailure (the `completed` set and cost
    # totals were removed in the v0.1.4 audit — assert observable outcomes:
    # the ceiling log line records the cumulative cost that tripped it).
    err = capfd.readouterr().err
    assert "worker budget ceiling reached" in err
    assert "cost_usd=0.03" in err
    assert "worker completed" in err
    assert "worker failed" not in err


async def test_incomplete_turn_resumes_same_session_with_continuation_prompt(
    tmp_path, monkeypatch, capfd
):
    """issue #47: a turn returning `incomplete` + RESUME_SESSION continues the
    SAME session via --resume with CONTINUATION_PROMPT (not the task prompt),
    counting one orchestrator turn — not a failure. Scripts incomplete -> success."""
    tmpl = WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 2")

    def factory(n, resume):
        if n == 1:
            return TurnResult(status="incomplete", session_id="inc-1",
                              continuation=Continuation.RESUME_SESSION, cost_usd=0.01)
        return TurnResult(status="succeeded", session_id="done-2", cost_usd=0.01)

    runner = ScriptedRunner(factory)
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, tmpl, runner=runner)
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1, "todo")}

    await orch._tick()
    tracker.candidates = []
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)

    assert len(runner.turns) == 2
    assert runner.turns[0][1] is None                 # turn 1: fresh session
    assert runner.turns[0][2] == "Work 1: Issue 1"    # ... rendered task prompt
    assert runner.turns[1][1] == "inc-1"              # turn 2 resumes the incomplete session
    assert runner.turns[1][2] == CONTINUATION_PROMPT  # ... with continuation, not task, prompt
    err = capfd.readouterr().err
    assert "worker completed" in err
    assert "worker failed" not in err


async def test_incomplete_leaves_turn_succeeded_false_and_cancellable(
    tmp_path, monkeypatch
):
    """issue #47 / #61 boundary: an `incomplete` turn must NOT set
    turn_succeeded (worker still active and cancellable, not a terminal
    handoff). Holds inside turn 2 to inspect mid-continuation state, then
    proves a terminal reconcile still cancels the worker."""
    tmpl = WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 5")

    def factory(n, resume):
        return TurnResult(status="incomplete", session_id=f"inc-{n}",
                          continuation=Continuation.RESUME_SESSION, cost_usd=0.0)

    runner = ScriptedRunner(factory, hold_on_turn=2)
    orch, tracker, _, ws_root = _build_harness(tmp_path, monkeypatch, tmpl, runner=runner)
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1, "todo")}

    await orch._tick()
    # Worker is blocked inside turn 2 (turn 1 already returned incomplete).
    await wait_for(lambda: len(runner.turns) == 2)
    entry = orch.running["node-1"]
    assert entry.turn_succeeded is False              # incomplete is not a terminal success
    assert runner.turns[1][1] == "inc-1"              # turn 2 resumed the incomplete session

    # Still cancellable: a terminal state reconcile takes authority immediately.
    tracker.states = {"node-1": make_issue(1, "closed")}
    await orch._reconcile_running()
    assert "node-1" not in orch.running
    runner.release.set()
    await wait_for(lambda: "node-1" not in orch.claimed)


async def test_incomplete_streak_terminates_at_max_turns(tmp_path, monkeypatch, capfd):
    """issue #47: a run of `incomplete` turns still terminates the session at
    agent.max_turns — each continuation consumes one orchestrator turn."""
    tmpl = WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 3")

    def factory(n, resume):
        return TurnResult(status="incomplete", session_id=f"inc-{n}",
                          continuation=Continuation.RESUME_SESSION, cost_usd=0.0)

    runner = ScriptedRunner(factory)
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, tmpl, runner=runner)
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1, "todo")}

    await orch._tick()
    tracker.candidates = []
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)

    assert len(runner.turns) == 3                     # bounded by agent.max_turns, not unbounded
    assert runner.turns[2][1] == "inc-2"              # each turn resumed the previous session
    err = capfd.readouterr().err
    assert "worker completed" in err
    assert "worker failed" not in err


async def test_incomplete_streak_terminates_at_budget_ceiling(tmp_path, monkeypatch, capfd):
    """issue #47: continuation resets no budget — a run of `incomplete` turns at
    $0.01 each still trips the $0.025 cumulative ceiling after turn 3, well
    before agent.max_turns (10)."""
    tmpl = (WORKFLOW_TMPL
            .replace("max_turns: 1", "max_turns: 10")
            .replace('command: "unused-by-fake-runner"',
                     'command: "unused-by-fake-runner"\n  max_budget_usd: 0.025'))

    def factory(n, resume):
        return TurnResult(status="incomplete", session_id=f"inc-{n}",
                          continuation=Continuation.RESUME_SESSION, cost_usd=0.01)

    runner = ScriptedRunner(factory)
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, tmpl, runner=runner)
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1, "todo")}

    await orch._tick()
    tracker.candidates = []
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)

    assert len(runner.turns) == 3                     # ceiling (0.03 >= 0.025), not max_turns
    err = capfd.readouterr().err
    assert "worker budget ceiling reached" in err
    assert "cost_usd=0.03" in err
    assert "worker completed" in err
    assert "worker failed" not in err


async def test_incomplete_without_continuation_is_failure(tmp_path, monkeypatch):
    """issue #47 defensive: an `incomplete` result the scheduler can't continue
    (no resumable session) falls back to today's failure/backoff semantics."""
    def factory(n, resume):
        return TurnResult(status="incomplete", session_id=None,
                          continuation=Continuation.RESUME_SESSION,
                          error="error_max_turns")

    runner = ScriptedRunner(factory)
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, runner=runner)
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}

    await orch._tick()
    await wait_for(lambda: "node-1" in orch.retry_attempts)
    assert orch.retry_attempts["node-1"].attempt == 1


@pytest.mark.parametrize("status", ["failed", "timed_out"])
async def test_non_incomplete_statuses_keep_failure_semantics(
    tmp_path, monkeypatch, status
):
    """issue #47 regression pin: every non-succeeded, non-incomplete status
    still fails and schedules a retry — the new branch narrows nothing else."""
    def factory(n, resume):
        return TurnResult(status=status, session_id=None, error="boom")

    runner = ScriptedRunner(factory)
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, runner=runner)
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}

    await orch._tick()
    await wait_for(lambda: "node-1" in orch.retry_attempts)
    assert orch.retry_attempts["node-1"].attempt == 1


async def test_maybe_reload_detects_real_mtime_change(harness):
    """_maybe_reload must pick up an edited workflow via the REAL stat path —
    no _workflow_mtime=None bypass. Also documents the granularity edge: an
    edit that lands with an IDENTICAL st_mtime (e.g. two writes within the
    filesystem's timestamp resolution) is invisible to mtime-based reload."""
    orch, _, _, _ = harness
    wf = orch.workflow_path
    assert orch._cfg.agent().max_concurrent_agents == 2
    orig = wf.stat()

    new_text = wf.read_text().replace("max_concurrent_agents: 2",
                                      "max_concurrent_agents: 5")
    wf.write_text(new_text)
    # Pin the mtime back to the original value: same-second (same-resolution)
    # edit. KNOWN LIMITATION — the reload path cannot see this change.
    os.utime(wf, ns=(orig.st_atime_ns, orig.st_mtime_ns))
    orch._maybe_reload()
    assert orch._cfg.agent().max_concurrent_agents == 2

    # A real mtime change is picked up without any test-harness bypass.
    os.utime(wf, ns=(orig.st_atime_ns, orig.st_mtime_ns + 1_000_000_000))
    orch._maybe_reload()
    assert orch._cfg.agent().max_concurrent_agents == 5
    assert orch._workflow_broken is None


async def test_worker_failure_uses_backoff_then_releases_when_gone(harness):
    orch, tracker, runner, _ = harness

    async def failing_turn(workspace, prompt, resume_session_id, on_event,
                           issue_id, agent_token=None):
        return TurnResult(status="failed", session_id=None, error="error_during_execution")

    runner.run_turn = failing_turn
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    await wait_for(lambda: "node-1" in orch.retry_attempts)
    assert orch.retry_attempts["node-1"].attempt == 1

    tracker.candidates = []  # issue disappears -> retry path releases the claim
    await wait_for(lambda: "node-1" not in orch.claimed
                   and "node-1" not in orch.retry_attempts)


# --- credential provider wiring (issue #10) -----------------------------------


async def test_components_share_one_credential_provider(tmp_path, monkeypatch):
    """Every tracker construction must reuse the process-lifetime provider —
    a per-tick provider would lose the mint cache and re-mint every poll."""
    import httpx

    ws_root = tmp_path / "ws"
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(WORKFLOW_TMPL.format(ws_root=ws_root))
    orch = Orchestrator(wf)
    orch._load_workflow(initial=True)
    async with httpx.AsyncClient() as client:
        orch._http = client
        orch._build_creds()
        assert orch._creds is not None
        t1, _ = orch._components()
        t2, _ = orch._components()
        assert t1._creds is orch._creds
        assert t2._creds is orch._creds
    orch._http = None


class FakeCredsProvider:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.mints = 0
        self.min_ttls: list[float] = []  # min_ttl requested per token() call

    async def token(self, *, min_ttl: float = 0.0) -> str:
        if self.fail:
            raise RuntimeError("mint endpoint unreachable")
        self.min_ttls.append(min_ttl)
        self.mints += 1
        return f"ghs-mint-{self.mints}"

    def invalidate(self) -> None:
        pass


async def test_worker_passes_minted_token_to_each_turn(harness):
    orch, tracker, runner, _ = harness
    orch._creds = FakeCredsProvider()
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    await wait_for(lambda: len(runner.turns) >= 1)
    await asyncio.gather(*(e.task for e in orch.running.values()),
                         return_exceptions=True)

    assert runner.tokens == ["ghs-mint-1"]


async def test_mint_failure_fails_worker_without_launching_agent(harness):
    orch, tracker, runner, _ = harness
    orch._creds = FakeCredsProvider(fail=True)
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    tasks = [e.task for e in orch.running.values()]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert runner.turns == []  # agent never launched without credentials
    assert any(isinstance(r, scheduler_mod.WorkerFailure) for r in results)


async def test_agent_token_requests_ttl_covering_the_turn(harness):
    # Codex PR #42 P1: the scheduler must demand a token that outlives the
    # turn (min_ttl = claude.turn_timeout), not just the tracker's 300s skew.
    orch, tracker, runner, _ = harness
    creds = FakeCredsProvider()
    orch._creds = creds
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    await wait_for(lambda: len(runner.turns) >= 1)
    await asyncio.gather(*(e.task for e in orch.running.values()),
                         return_exceptions=True)

    # WORKFLOW_TMPL sets claude.turn_timeout_ms: 5000
    assert creds.min_ttls == [5.0]


# --- Stage 5A Codex-only process canary --------------------------------------

async def test_codex_mode_dispatches_continues_and_uses_codex_policy(
    tmp_path,
    monkeypatch,
    capfd,
):
    tmpl = CODEX_WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 2")
    runner = FakeRunner()
    runner.provider_id = "codex"
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=tmpl,
        runner=runner,
        provider_id="codex",
    )
    creds = FakeCredsProvider()
    orch._creds = creds
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    tracker.candidates = []
    await wait_for(
        lambda: not orch.running
        and not orch.retry_attempts
        and issue.id not in orch.claimed
    )

    assert len(runner.turns) == 2
    assert runner.turns[0][1] is None
    assert runner.turns[1][1] == "sess-1"
    assert runner.turns[1][2] == CONTINUATION_PROMPT
    assert runner.tokens == ["ghs-mint-1", "ghs-mint-2"]
    assert creds.min_ttls == [7.0, 7.0]
    err = capfd.readouterr().err
    assert "provider_id=codex" in err
    assert "worker completed" in err


async def test_codex_mode_failure_retries_and_releases_claim(
    tmp_path,
    monkeypatch,
    capfd,
):
    runner = FakeRunner()
    runner.provider_id = "codex"

    async def failing_turn(
        workspace,
        prompt,
        resume_session_id,
        on_event,
        issue_id,
        agent_token=None,
    ):
        return TurnResult(status="failed", error="codex canary failure")

    runner.run_turn = failing_turn
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=CODEX_WORKFLOW_TMPL,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    await wait_for(lambda: issue.id in orch.retry_attempts)
    tracker.candidates = []
    await wait_for(
        lambda: issue.id not in orch.retry_attempts and issue.id not in orch.claimed
    )

    err = capfd.readouterr().err
    assert "provider_id=codex" in err
    assert "worker failed" in err
    assert "outcome=failed" in err
    assert "failure_class=worker_failure" in err


async def test_codex_provider_failure_waits_without_burning_retry_or_session(
    tmp_path,
    monkeypatch,
    capfd,
):
    runner = FakeRunner()
    runner.provider_id = "codex"
    calls = 0

    async def failing_turn(*args, **kwargs):
        nonlocal calls
        calls += 1
        return TurnResult(
            status="failed",
            session_id=None,
            error="codex_error",
            failure_class=FailureClass.PROVIDER_PLAN_LIMIT,
        )

    runner.run_turn = failing_turn
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=CODEX_WORKFLOW_TMPL,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    await wait_for(lambda: issue.id in orch.provider_waiting)

    assert issue.id not in orch.retry_attempts
    assert issue.id not in orch.sessions_per_issue
    assert issue.id in orch.claimed
    assert issue.id not in orch.parked
    assert orch.provider_waiting[issue.id].retry_attempt is None
    assert orch.provider_circuits["codex"].state is CircuitState.OPEN_LATCHED

    # The same candidate remains claimed and provider-pinned, but another poll
    # cannot launch it while the provider circuit is open.
    await orch._tick()
    await asyncio.sleep(0)
    assert calls == 1

    err = capfd.readouterr().err
    assert "provider_id=codex" in err
    assert "outcome=failed" in err
    assert "failure_class=provider_plan_limit" in err
    assert "retry_disposition=provider_wait" in err
    assert "circuit_state=open_latched" in err
    assert err.count("provider circuit blocked dispatch") == 1


def assigned_issue(n: int, provider_id: str, state: str = "todo") -> Issue:
    issue = make_issue(n, state)
    issue.labels.append(f"provider:{provider_id}")
    return issue


async def test_open_codex_circuit_does_not_cancel_running_work_or_block_claude(
    tmp_path,
    monkeypatch,
):
    codex = FakeRunner()
    codex.provider_id = "codex"
    claude = FakeRunner()
    claude.provider_id = "claude"
    failure_release = asyncio.Event()
    success_release = asyncio.Event()

    async def codex_turn(*args, issue_id, **kwargs):
        if issue_id == "node-1":
            await failure_release.wait()
            return TurnResult(
                status="failed",
                session_id=None,
                error="plan unavailable",
                failure_class=FailureClass.PROVIDER_PLAN_LIMIT,
            )
        await success_release.wait()
        return TurnResult(status="succeeded", session_id="healthy-codex")

    codex.run_turn = codex_turn
    selector = MixedFixedRunnerSelector({"claude": claude, "codex": codex})
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=MIXED_WORKFLOW_TMPL,
        runner=codex,
        runner_selector=selector,
    )
    failing = assigned_issue(1, "codex")
    already_running = assigned_issue(2, "codex")
    tracker.candidates = [failing, already_running]
    tracker.states = {issue.id: issue for issue in tracker.candidates}

    await orch._tick()
    await wait_for(lambda: set(orch.running) == {failing.id, already_running.id})
    failure_release.set()
    await wait_for(lambda: failing.id in orch.provider_waiting)
    assert already_running.id in orch.running
    assert not orch.running[already_running.id].task.cancelled()
    assert orch.provider_circuits["codex"].state is CircuitState.OPEN_LATCHED

    peer = assigned_issue(3, "claude")
    tracker.candidates.append(peer)
    tracker.states[peer.id] = peer
    await orch._tick()
    await wait_for(lambda: any(turn[0] == peer.id for turn in claude.turns))

    success_release.set()
    await wait_for(
        lambda: orch.provider_circuits["codex"].state is CircuitState.CLOSED
    )


async def test_half_open_runs_one_probe_then_drains_waiters_oldest_first(
    tmp_path,
    monkeypatch,
):
    codex = FakeRunner(hold=True)
    codex.provider_id = "codex"
    claude = FakeRunner()
    claude.provider_id = "claude"
    selector = MixedFixedRunnerSelector({"claude": claude, "codex": codex})
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=MIXED_WORKFLOW_TMPL,
        runner=codex,
        runner_selector=selector,
    )
    now = [0.0]
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=lambda: now[0])
    circuit.record_failure(FailureClass.PROVIDER_UNAVAILABLE)
    orch.provider_circuits["codex"] = circuit
    issues = [assigned_issue(n, "codex", "in progress") for n in (1, 2, 3)]
    tracker.candidates = list(reversed(issues))
    tracker.states = {issue.id: issue for issue in issues}
    queued = datetime(2026, 7, 1, tzinfo=UTC)
    for offset, issue in enumerate(issues):
        orch.claimed.add(issue.id)
        orch.provider_waiting[issue.id] = ProviderWaitEntry(
            issue.identifier,
            issue,
            "codex",
            retry_attempt=7,
            queued_at=queued + timedelta(seconds=offset),
        )

    now[0] = 1.0
    await orch._resume_provider_waiters(tracker.candidates)
    await wait_for(lambda: len(codex.turns) == 1)
    assert [turn[0] for turn in codex.turns] == [issues[0].id]
    assert circuit.state is CircuitState.HALF_OPEN
    assert set(orch.provider_waiting) == {issues[1].id, issues[2].id}

    await orch._resume_provider_waiters(tracker.candidates)
    assert [turn[0] for turn in codex.turns] == [issues[0].id]

    codex.release.set()
    await wait_for(lambda: circuit.state is CircuitState.CLOSED)
    codex.release.clear()
    await orch._resume_provider_waiters(tracker.candidates)
    await wait_for(lambda: len(codex.turns) >= 3)
    assert [turn[0] for turn in codex.turns[:3]] == [
        issues[0].id,
        issues[1].id,
        issues[2].id,
    ]
    assert set(orch.running) == {issues[1].id, issues[2].id}
    assert all(orch.running[issue.id].retry_attempt == 7 for issue in issues[1:])
    codex.release.set()


async def test_successful_half_open_handoff_finishes_before_reconciliation(
    tmp_path,
    monkeypatch,
    capfd,
):
    """A handoff label written by a successful probe must not race finalization.

    The tracker may expose status:human-review while the worker is still in its
    after_run hook. Reconciliation must let that already-successful worker exit
    normally so the half-open circuit closes instead of treating the probe as
    abandoned and reopening cooldown.
    """
    codex = FakeRunner()
    codex.provider_id = "codex"
    claude = FakeRunner()
    claude.provider_id = "claude"
    selector = MixedFixedRunnerSelector({"claude": claude, "codex": codex})
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=MIXED_WORKFLOW_TMPL,
        runner=codex,
        runner_selector=selector,
    )
    hook_started, hook_release = _wedged_after_run(monkeypatch)
    now = [0.0]
    circuit = ProviderCircuit("codex", cooldown_ms=1000, clock=lambda: now[0])
    circuit.record_failure(FailureClass.PROVIDER_UNAVAILABLE)
    orch.provider_circuits["codex"] = circuit
    issue = assigned_issue(1, "codex", "in progress")
    tracker.candidates = [issue]
    tracker.states = {issue.id: issue}
    orch.claimed.add(issue.id)
    orch.provider_waiting[issue.id] = ProviderWaitEntry(
        issue.identifier,
        issue,
        "codex",
        retry_attempt=None,
    )

    now[0] = 1.0
    await orch._resume_provider_waiters(tracker.candidates)
    await wait_for(hook_started.is_set)
    assert circuit.state is CircuitState.HALF_OPEN
    assert orch.running[issue.id].turn_succeeded
    orch.running[issue.id].stall_timeout_ms = 1
    orch.running[issue.id].last_event_at = datetime.now(UTC) - timedelta(hours=1)

    handed_off = assigned_issue(1, "codex", "human review")
    tracker.states = {issue.id: handed_off}
    await orch._reconcile_running()

    assert issue.id in orch.running
    assert not orch.running[issue.id].task.cancelled()
    assert circuit.state is CircuitState.HALF_OPEN

    tracker.candidates = []
    hook_release.set()
    await wait_for(lambda: circuit.state is CircuitState.CLOSED)
    await wait_for(lambda: issue.id not in orch.running)

    err = capfd.readouterr().err
    assert "successful worker finalizing; deferring reconciliation" in err
    assert "worker completed" in err
    assert "worker cancelled" not in err


async def test_restart_outage_is_bounded_by_provider_capacity_and_refunds_batch(
    tmp_path,
    monkeypatch,
):
    codex = FakeRunner(hold=True)
    codex.provider_id = "codex"
    claude = FakeRunner()
    claude.provider_id = "claude"
    codex_calls = 0

    async def unavailable_turn(*args, **kwargs):
        nonlocal codex_calls
        codex_calls += 1
        await codex.release.wait()
        return TurnResult(
            status="failed",
            session_id=None,
            error="provider unavailable",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )

    codex.run_turn = unavailable_turn
    selector = MixedFixedRunnerSelector({"claude": claude, "codex": codex})
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=MIXED_WORKFLOW_TMPL,
        runner=codex,
        runner_selector=selector,
    )
    issues = [assigned_issue(n, "codex") for n in range(1, 6)]
    tracker.candidates = issues
    tracker.states = {issue.id: issue for issue in issues}

    await orch._tick()
    assert len(orch.running) == 2
    assert set(orch.sessions_per_issue) == {issues[0].id, issues[1].id}
    codex.release.set()
    await wait_for(lambda: len(orch.provider_waiting) == 2)

    assert orch.provider_circuits["codex"].state is CircuitState.OPEN_COOLDOWN
    assert orch.sessions_per_issue == {}
    assert all(
        waiter.retry_attempt is None
        for waiter in orch.provider_waiting.values()
    )
    await orch._tick()
    await asyncio.sleep(0)
    assert len(orch.provider_waiting) == 2
    assert codex_calls == 2
    assert all(issue.id not in orch.sessions_per_issue for issue in issues)


async def test_retry_maturing_during_open_circuit_preserves_attempt_in_wait(
    tmp_path,
    monkeypatch,
):
    runner = FakeRunner()
    runner.provider_id = "codex"
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=CODEX_WORKFLOW_TMPL,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue
    orch._provider_circuit("codex").record_failure(
        FailureClass.PROVIDER_AUTHENTICATION)
    orch._schedule_retry(issue.id, issue.identifier, attempt=4, delay_ms=60_000)
    orch.retry_attempts[issue.id].timer_handle.cancel()

    await orch._on_retry_timer(issue.id)

    assert issue.id not in orch.retry_attempts
    assert issue.id in orch.claimed
    assert issue.id in orch.provider_waiting
    assert orch.provider_waiting[issue.id].retry_attempt == 4
    assert issue.id not in orch.sessions_per_issue
    assert runner.turns == []


@pytest.mark.parametrize(
    ("next_state", "workspace_preserved", "release_reason"),
    [
        ("closed", False, "terminal"),
        ("human review", True, "no_longer_candidate"),
    ],
)
async def test_provider_waiter_reconciliation_releases_without_resurrection(
    tmp_path,
    monkeypatch,
    capfd,
    next_state,
    workspace_preserved,
    release_reason,
):
    runner = FakeRunner()
    runner.provider_id = "codex"

    async def failing_turn(*args, **kwargs):
        return TurnResult(
            status="failed",
            session_id=None,
            error="provider unavailable",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )

    runner.run_turn = failing_turn
    orch, tracker, _, ws_root = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=CODEX_WORKFLOW_TMPL,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    await wait_for(lambda: issue.id in orch.provider_waiting)
    workspace = ws_root / issue.identifier
    assert workspace.is_dir()

    tracker.candidates = []
    tracker.states[issue.id] = make_issue(1, next_state)
    await orch._tick()

    assert issue.id not in orch.provider_waiting
    assert issue.id not in orch.claimed
    assert workspace.exists() is workspace_preserved
    err = capfd.readouterr().err
    assert "provider wait released" in err
    assert f"reason={release_reason}" in err


async def test_terminal_provider_waiter_reconciles_while_capacity_is_full(
    tmp_path,
    monkeypatch,
):
    runner = FakeRunner()
    runner.provider_id = "codex"

    async def failing_turn(*args, **kwargs):
        return TurnResult(
            status="failed",
            session_id=None,
            error="provider unavailable",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )

    runner.run_turn = failing_turn
    orch, tracker, _, ws_root = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=CODEX_WORKFLOW_TMPL,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue
    await orch._tick()
    await wait_for(lambda: issue.id in orch.provider_waiting)

    workspace = ws_root / issue.identifier
    tracker.candidates = []
    tracker.states[issue.id] = make_issue(1, "closed")
    monkeypatch.setattr(orch, "_available_slots", lambda: 0)

    await orch._resume_provider_waiters([])

    assert issue.id not in orch.provider_waiting
    assert issue.id not in orch.claimed
    assert not workspace.exists()


async def test_ineligible_provider_waiter_reverts_visible_claim(
    tmp_path,
    monkeypatch,
):
    workflow = CODEX_WORKFLOW_TMPL.replace(
        '  terminal_states: ["done", "closed", "cancelled"]',
        '  terminal_states: ["done", "closed", "cancelled"]\n'
        '  required_labels: ["gate:triage-passed"]',
    )
    runner = FakeRunner()
    runner.provider_id = "codex"

    async def failing_turn(*args, **kwargs):
        return TurnResult(
            status="failed",
            session_id=None,
            error="provider unavailable",
            failure_class=FailureClass.PROVIDER_UNAVAILABLE,
        )

    runner.run_turn = failing_turn
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=workflow,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue
    await orch._tick()
    await wait_for(lambda: issue.id in orch.provider_waiting)
    issue.labels.remove("gate:triage-passed")

    await orch._resume_provider_waiters([issue])

    assert issue.id not in orch.provider_waiting
    assert issue.id not in orch.claimed
    assert TODO_LABEL in issue.labels
    assert IN_PROGRESS_LABEL not in issue.labels
    assert tracker.comments == [(issue.id, CLAIM_RELEASE_COMMENT)]


async def test_codex_mode_enforces_capacity_and_cancels_terminal_worker(
    tmp_path,
    monkeypatch,
    capfd,
):
    tmpl = CODEX_WORKFLOW_TMPL.replace(
        "max_concurrent_agents: 2",
        "max_concurrent_agents: 1",
    )
    runner = FakeRunner(hold=True)
    runner.provider_id = "codex"
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=tmpl,
        runner=runner,
        provider_id="codex",
    )
    first = make_issue(1)
    second = make_issue(2)
    tracker.candidates = [first, second]
    tracker.states = {first.id: first, second.id: second}

    await orch._tick()
    assert list(orch.running) == [first.id]
    assert orch.running[first.id].provider_id == "codex"

    tracker.candidates = []
    tracker.states[first.id] = make_issue(1, "closed")
    await orch._reconcile_running()
    assert first.id not in orch.running
    await wait_for(lambda: first.id not in orch.claimed)
    err = capfd.readouterr().err
    assert "provider_id=codex" in err
    assert "outcome=cancelled" in err


async def test_codex_mode_parks_after_session_cap(tmp_path, monkeypatch):
    tmpl = CODEX_WORKFLOW_TMPL.replace(
        "max_sessions_per_issue: 2",
        "max_sessions_per_issue: 1",
    )
    runner = FakeRunner()
    runner.provider_id = "codex"
    orch, tracker, _, _ = _build_harness(
        tmp_path,
        monkeypatch,
        workflow_tmpl=tmpl,
        runner=runner,
        provider_id="codex",
    )
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue

    await orch._tick()
    await wait_for(lambda: issue.id in orch.parked)

    assert "status:parked" in issue.labels
    assert issue.id not in orch.running
    assert issue.id not in orch.claimed


# --- claim-visibility labels (issue #14 / AgDR-010) ---------------------------
#
# status:in-progress is board visibility only, NOT a lock: applied once when a
# `todo` issue is first claimed, cleared when the claim genuinely dies. The
# label tracks the CLAIM, not the session — continuations/retries write nothing.

ALLOWED_STATUS_LABELS = {"status:todo", "status:in-progress", "status:parked"}
FORBIDDEN_STATUS_LABELS = {  # gate/handoff/triage labels the orchestrator owns NONE of
    "status:drafting", "status:plan-review", "status:human-review",
    "status:blocked", "status:triage",
}


def _labels_written(tracker) -> set[str]:
    return {lbl for _, names in (tracker.labels_added + tracker.labels_removed)
            for lbl in names}


async def test_todo_dispatch_label_write_costs_no_session(tmp_path, monkeypatch):
    """AC (role-pin regression, AgDR-010 decision #3): the orchestrator's own
    status:todo -> status:in-progress write must NOT trip the between-turn
    role-pin break. A multi-turn `todo` engagement runs its turns in ONE session,
    exactly as an equivalent `in progress` dispatch does — and writes the
    in-progress label exactly once. The write-count AC alone would not catch this
    (a forced turn-1 break still writes exactly once); this asserts session parity.
    """
    tmpl = WORKFLOW_TMPL.replace("max_turns: 1", "max_turns: 3")

    # todo dispatch: label swap happens, then 3 turns in one session.
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    tracker.candidates = [make_issue(1, "todo")]
    tracker.states = {"node-1": make_issue(1, "todo")}
    await orch._tick()
    tracker.candidates = []  # quiesce: post-session retry finds no candidate
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)
    assert len(runner.turns) == 3                       # ONE session, no forced break
    todo_sessions = orch.sessions_per_issue.get("node-1")
    assert todo_sessions == 1
    assert tracker.labels_added == [("node-1", ("status:in-progress",))]   # once
    assert tracker.labels_removed == [("node-1", ("status:todo",))]        # once

    # in progress dispatch: same session count, and NO status-label writes at all.
    orch2, tracker2, runner2, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    tracker2.candidates = [make_issue(1, "in progress")]
    tracker2.states = {"node-1": make_issue(1, "in progress")}
    await orch2._tick()
    tracker2.candidates = []
    await wait_for(lambda: not orch2.running and not orch2.retry_attempts
                   and "node-1" not in orch2.claimed)
    assert len(runner2.turns) == 3
    assert orch2.sessions_per_issue.get("node-1") == todo_sessions   # PARITY
    assert tracker2.labels_added == []                  # already in-progress: no write
    assert tracker2.labels_removed == []


async def test_failure_retries_do_not_reflap_in_progress_label(tmp_path, monkeypatch):
    """AC: the label tracks the CLAIM, not the session — between-session backoff
    must not flap it. A `todo` issue whose sessions keep FAILING writes
    status:in-progress exactly once across every failure retry; park then swaps
    in the durable marker. Asserts the TOTAL label-write set."""
    tmpl = WORKFLOW_TMPL.replace("max_sessions_per_issue: 2", "max_sessions_per_issue: 3")
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)

    async def failing_turn(workspace, prompt, resume_session_id, on_event,
                           issue_id, agent_token=None):
        return TurnResult(status="failed", session_id=None, error="boom")
    runner.run_turn = failing_turn

    issue = make_issue(1, "todo")
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()
    await wait_for(lambda: "node-1" in orch.parked)     # 3 failed sessions -> park

    assert orch.sessions_per_issue.get("node-1") == 3   # cap spent on failures
    assert tracker.labels_added == [
        ("node-1", ("status:in-progress",)),            # first dispatch: claim visible
        ("node-1", ("status:parked",)),                 # durable park marker
    ]
    assert tracker.labels_removed.count(("node-1", ("status:todo",))) == 1
    assert ("node-1", ("status:in-progress",)) in tracker.labels_removed   # cleared at park


async def test_triage_dispatch_writes_no_status_label(tmp_path, monkeypatch):
    """AC: a `triage`-state first dispatch performs ZERO status-label writes —
    status:triage is verifier-owned and must not be clobbered (a verifier session
    would lose its role pin)."""
    tmpl = WORKFLOW_TMPL.replace('active_states: ["todo", "in progress"]',
                                 'active_states: ["triage", "todo", "in progress"]')
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    issue = make_issue(1, "triage")
    tracker.candidates = [issue]
    tracker.states = {"node-1": make_issue(1, "triage")}

    await orch._tick()
    tracker.candidates = []
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)
    assert runner.turns                                 # it WAS dispatched
    assert tracker.labels_added == []
    assert tracker.labels_removed == []
    assert "status:triage" in issue.labels              # untouched


async def test_release_in_progress_claim_reverts_with_comment(harness):
    """AC: the shared revert helper flips status:in-progress -> status:todo and,
    with comment=True (mid-run claim release), posts ONE honest one-line note."""
    orch, tracker, _, _ = harness
    issue = make_issue(1, "in progress")                # sole status:in-progress
    await orch._release_in_progress_claim(tracker, issue, comment=True)
    assert tracker.labels_added == [("node-1", ("status:todo",))]
    assert tracker.labels_removed == [("node-1", ("status:in-progress",))]
    assert issue.state == "todo"
    assert issue.labels == ["status:todo"]
    assert len(tracker.comments) == 1
    assert "released its claim" in tracker.comments[0][1].lower()


async def test_startup_sweep_reverts_stranded_claim_comment_free(harness):
    """AC / AgDR-010 decision #5: an open status:in-progress issue with no live
    claim is a lie on the board (a prior process crashed mid-run). The startup
    sweep reverts it to status:todo but posts NO comment — the next tick may
    immediately re-dispatch it, so a "nobody's working this" note would be noise."""
    orch, tracker, _, _ = harness
    stranded = make_issue(1, "in progress")
    tracker.candidates = [stranded]                     # not in running/claimed/retry
    await orch._startup_in_progress_sweep()
    assert ("node-1", ("status:todo",)) in tracker.labels_added
    assert ("node-1", ("status:in-progress",)) in tracker.labels_removed
    assert stranded.state == "todo"
    assert tracker.comments == []                       # comment-free (decision #5)


async def test_startup_sweep_skips_live_claim(harness):
    """The sweep only reverts STRANDED claims: an in-progress issue THIS process
    still holds (running/claimed/retry) is left untouched."""
    orch, tracker, _, _ = harness
    held = make_issue(1, "in progress")
    tracker.candidates = [held]
    orch.claimed.add("node-1")                          # a live claim owns it
    await orch._startup_in_progress_sweep()
    assert tracker.labels_added == []
    assert tracker.labels_removed == []
    assert held.state == "in progress"


async def test_revert_skips_when_status_already_moved(harness):
    """AC: both revert paths NO-OP when the issue's status was already moved by a
    human/agent (e.g. an agent handoff to status:human-review). The board already
    reflects a real transition, so the orchestrator leaves it alone."""
    orch, tracker, _, _ = harness
    moved = make_issue(1, "human review")               # not sole status:in-progress
    await orch._release_in_progress_claim(tracker, moved, comment=True)
    assert tracker.labels_added == []
    assert tracker.labels_removed == []
    assert tracker.comments == []
    assert moved.labels == ["status:human-review"]


async def test_revert_skips_closed_issue(harness):
    """A claim whose issue closed out from under it is not reverted (labels are
    only meaningful while the issue is open)."""
    orch, tracker, _, _ = harness
    closed = make_issue(1, "in progress")
    closed.state = "closed"
    await orch._release_in_progress_claim(tracker, closed, comment=True)
    assert tracker.labels_added == []
    assert tracker.labels_removed == []
    assert tracker.comments == []


async def test_orchestrator_never_writes_gate_or_handoff_labels(harness):
    """AC (guard over the label-writing call sites): across a full engagement —
    todo dispatch, an agent handoff to status:human-review mid-session, and the
    subsequent claim-release check — every label the orchestrator writes is one
    of its OWNED three, and it never adds/removes a gate/handoff/triage label nor
    reverts the human's handoff."""
    orch, tracker, runner, _ = harness
    issue = make_issue(1, "todo")
    tracker.candidates = [issue]
    # agent hands the issue off to human-review during turn 1 (role-pin ends it).
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    # the world now shows human-review everywhere: the retry check must skip.
    tracker.candidates = [make_issue(1, "human review")]
    await wait_for(lambda: not orch.running and not orch.retry_attempts
                   and "node-1" not in orch.claimed)

    written = _labels_written(tracker)
    assert written <= ALLOWED_STATUS_LABELS             # only owned labels ever touched
    assert not (written & FORBIDDEN_STATUS_LABELS)      # never a gate/handoff/triage label
    assert tracker.comments == []                       # handoff not "released" — left alone

# --- issue #61: terminal-safe handoff (orchestrator-owned transition) ---------

import contextlib as _contextlib  # noqa: E402  (test-local, issue #61 block)
import json as _json  # noqa: E402
import subprocess as _subprocess  # noqa: E402


def _git_ws(ws: Path) -> str:
    """Make `ws` a real git repo (the handoff validator runs real git) and
    return its HEAD sha."""
    for args in (["init", "-q", "-b", "switchboard/issue-1"],
                 ["-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "seed"]):
        _subprocess.run(["git", "-C", str(ws), *args], check=True,
                        capture_output=True)
    out = _subprocess.run(["git", "-C", str(ws), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True)
    return out.stdout.strip()


class HandoffRunner(FakeRunner):
    """Succeeds like FakeRunner, but first git-inits the workspace and writes
    the production evidence contract — the worker behavior the issue #61
    prompt mandates. The paired FakeTracker (attached post-harness via
    `.tracker`) gets a matching open PR. `evidence_overrides` injects defects;
    `fail_with_evidence=True` reproduces the Stage 7 race (evidence exists,
    provider result is a failure)."""

    def __init__(self, *, evidence_overrides=None, fail_with_evidence=False):
        super().__init__()
        self.tracker: FakeTracker | None = None
        self.evidence_overrides = evidence_overrides or {}
        self.fail_with_evidence = fail_with_evidence

    async def run_turn(self, workspace, prompt, resume_session_id, on_event,
                       issue_id, agent_token=None):
        head = _git_ws(Path(workspace))
        run_dir = Path(workspace) / ".run"
        run_dir.mkdir(exist_ok=True)
        evidence = {"issue": "1", "pr_number": 5, "head_sha": head,
                    **self.evidence_overrides}
        (run_dir / "handoff-evidence.json").write_text(
            _json.dumps(evidence), encoding="utf-8")
        assert self.tracker is not None
        self.tracker.open_prs["switchboard/issue-1"] = [
            {"number": 5, "head_sha": head, "closes": [1]}]
        result = await super().run_turn(
            workspace, prompt, resume_session_id, on_event, issue_id,
            agent_token=agent_token)
        if self.fail_with_evidence:
            return TurnResult(status="failed", session_id=result.session_id,
                              error="provider died after evidence was written")
        return result


def _handoff_harness(tmp_path, monkeypatch, **runner_kw):
    runner = HandoffRunner(**runner_kw)
    orch, tracker, _, ws_root = _build_harness(tmp_path, monkeypatch,
                                               runner=runner)
    runner.tracker = tracker
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states = {issue.id: issue}
    return orch, tracker, issue


async def test_valid_handoff_swaps_to_sole_human_review(tmp_path, monkeypatch):
    orch, tracker, issue = _handoff_harness(tmp_path, monkeypatch)
    await orch._tick()
    await wait_for(lambda: tracker.sole_status_swaps)
    await wait_for(lambda: not orch.running)
    status = sorted(l for l in issue.labels if l.startswith("status:"))
    assert status == ["status:human-review"]
    assert tracker.sole_status_swaps == [(issue.id, "status:human-review")]
    # the triage provenance marker is not a status label and survives
    assert "gate:triage-passed" in issue.labels


async def test_stage7_race_failure_after_evidence_never_exposes_human_review(
        tmp_path, monkeypatch):
    orch, tracker, issue = _handoff_harness(
        tmp_path, monkeypatch, fail_with_evidence=True)
    await orch._tick()
    await wait_for(lambda: not orch.running)
    assert tracker.sole_status_swaps == []
    assert "status:human-review" not in issue.labels


async def test_invalid_evidence_is_diagnostic_not_transition(
        tmp_path, monkeypatch, capfd):
    orch, tracker, issue = _handoff_harness(
        tmp_path, monkeypatch, evidence_overrides={"head_sha": "0" * 40})
    await orch._tick()
    await wait_for(lambda: not orch.running)
    assert tracker.sole_status_swaps == []
    assert "status:human-review" not in issue.labels
    err = capfd.readouterr().err
    assert "handoff evidence rejected" in err
    assert "head_mismatch" in err
