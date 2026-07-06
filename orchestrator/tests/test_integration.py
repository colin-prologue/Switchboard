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
from orchestrator.scheduler import CONTINUATION_PROMPT, Orchestrator
from orchestrator.types import BlockerRef, Issue, TrackerError, TurnResult
from orchestrator.workspace import WorkspaceManager

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
        self.labels_added: list[tuple[str, tuple[str, ...]]] = []
        self.add_labels_error: TrackerError | None = None  # set to simulate a write failure

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
        for issue in (*self.candidates, *self.states.values()):
            if issue.id == issue_id:
                for name in label_names:
                    if name not in issue.labels:
                        issue.labels.append(name)


class FakeRunner:
    """Controllable runner: workers block until released, then succeed.

    Returns a distinct session id per turn (sess-1, sess-2, ...) so tests can
    assert the scheduler resumes with the LATEST session id, not a stale one.
    """

    def __init__(self, hold: bool = False):
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


def _build_harness(tmp_path, monkeypatch, workflow_tmpl=WORKFLOW_TMPL):
    monkeypatch.setattr(scheduler_mod, "CONTINUATION_DELAY_MS", 30)
    monkeypatch.setattr(scheduler_mod, "FAILURE_BASE_BACKOFF_MS", 30)
    ws_root = tmp_path / "ws"
    wf = tmp_path / "WORKFLOW.md"
    wf.write_text(workflow_tmpl.format(ws_root=ws_root))

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


async def test_stall_detection_terminates_and_retries(harness, monkeypatch):
    orch, tracker, runner, _ = harness
    # keep the retry entry observable (capped at 500ms) once teardown lands
    monkeypatch.setattr(scheduler_mod, "FAILURE_BASE_BACKOFF_MS", 10000)
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
    assert "node-1" in orch.claimed
    # §8.5: stall -> terminate + retry, scheduled once teardown reports back
    await wait_for(lambda: "node-1" in orch.retry_attempts)


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
    assert tracker.labels_added == [("node-1", ("status:parked",))]  # durable marker
    assert "status:parked" in issue.labels            # visible on future fetches
    assert (ws_root / "1").is_dir()                   # workspace preserved
    assert "node-1" not in orch.claimed
    assert "node-1" not in orch.retry_attempts

    await orch._tick()                                # still parked: no re-dispatch
    assert orch.sessions_per_issue.get("node-1", 0) == 2
    assert len(tracker.comments) == 1
    assert len(tracker.labels_added) == 1             # not re-labelled

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
    runner.hold = True
    tracker.candidates = [make_issue(1)]
    tracker.states = {"node-1": make_issue(1)}
    await orch._tick()
    await wait_for(lambda: runner.turns)    # worker genuinely inside run_turn
    orch.running["node-1"].started_at = datetime.now(UTC) - timedelta(hours=1)

    wf = orch.workflow_path
    wf.write_text(wf.read_text().replace("stall_timeout_ms: 0",
                                         "stall_timeout_ms: 1000"))
    orch._workflow_mtime = None
    orch._load_workflow(initial=False)

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
        t1, _, _ = orch._components()
        t2, _, _ = orch._components()
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
