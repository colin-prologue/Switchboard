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
from dataclasses import replace as dc_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import orchestrator.scheduler as scheduler_mod
from orchestrator.fold import FoldSignal
from orchestrator.fold_apply import (
    FOLD_MARKER_PREFIX,
    PROPOSAL_CLOSE,
    PROPOSAL_OPEN,
    apply_fold_signal,
    body_digest,
    marker_first_line,
)
from orchestrator.provider_circuit import CircuitState, ProviderCircuit
from orchestrator.scheduler import (
    CLAIM_RELEASE_COMMENT,
    CONTINUATION_PROMPT,
    IMPLEMENT_ROLE,
    IN_PROGRESS_LABEL,
    TODO_LABEL,
    VERIFY_ROLE,
    Orchestrator,
    ProviderWaitEntry,
)
from orchestrator.types import (
    BlockerRef,
    CommentReaction,
    Continuation,
    IssueComment,
    FailureClass,
    Issue,
    ReviewThread,
    ReviewThreadComment,
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
        self.issue_comments: dict[str, list] = {}  # issue number -> IssueComment
        # issue #43 review-response fixtures. Fidelity set (OBS-023): the fake
        # stores exactly what the server derives and the predicate reads —
        # `isResolved`, per-comment author login, comment `createdAt` — and
        # NEVER a hard-coded needs_response verdict. `pr_comments` is a separate
        # map from `issue_comments` because the real tracker needs a separate
        # query for it (repository.issue(number:) is null for a PR).
        self.pr_review_threads: dict[int, list] = {}  # PR number -> ReviewThread
        self.pr_comments: dict[int, list] = {}        # PR number -> IssueComment
        # Every network-shaped read/write bumps this. The disable-path AC is
        # "ZERO API calls", which is only assertable if the fake counts them.
        self.api_calls = 0
        self.sole_status_swaps: list[tuple[str, str]] = []
        self.add_labels_error: TrackerError | None = None  # set to simulate a write failure
        self.remove_labels_error: TrackerError | None = None
        # --- fold apply seams (issue #126) ---------------------------------
        # The body store IS `Issue.description` (fake fidelity, OBS-023: the
        # real tracker has no separate body field and derives the digest from
        # the same bytes `fetch_issue_states_by_ids` returns). These record and
        # perturb writes; the fake stays derive-faithful by default.
        self.body_writes: list[tuple[str, str]] = []
        self.update_body_error: TrackerError | None = None
        self.add_comment_error: TrackerError | None = None
        # ONE-SHOT divergence injection: perturbs the STORED bytes for exactly
        # one write. The only way a derive-faithful fake can exercise the
        # verify-after-write branch (AC 1) without lying about anything else.
        self.mangle_next_body_write = False
        # Call-ordinal seam for fetch_issue_states_by_ids. Scope is per APPLY
        # INVOCATION and the TEST owns that boundary — the fake cannot observe
        # invocation boundaries, only method calls, so a test resets
        # `states_calls` immediately before the apply it is arming.
        self.states_calls = 0
        self.states_error_at_ordinal: int | None = None

    async def fetch_candidate_issues(self):
        return list(self.candidates)

    async def fetch_issues_by_states(self, state_names):
        return list(self.terminal) if state_names else []

    async def fetch_open_issues_by_status(self, status_names):
        # Conformance note (OBS-023), issue #51: the real method derives its
        # result by filtering the SAME open-issue set on the normalized
        # `issue.state` — it does not maintain a separate per-status index. A
        # filterless fake would green-wash the defect this method exists to fix
        # (gate states are invisible to fetch_candidate_issues because they are
        # not active states), so the fake filters the same way.
        self.api_calls += 1
        wanted = {s.strip().lower() for s in status_names if s and s.strip()}
        if not wanted:
            return []
        return [i for i in self.candidates if i.state in wanted]

    async def fetch_issue_comments(self, issue_number):
        return list(self.issue_comments.get(str(issue_number), []))

    async def fetch_issue_states_by_ids(self, ids):
        self.states_calls += 1
        if self.states_error_at_ordinal == self.states_calls:
            raise TrackerError("github_api_status", "injected read failure")
        return [self.states[i] for i in ids if i in self.states]

    async def update_issue_body(self, issue_id, body):
        """Mirror of `updateIssue(input: {id, body})` (issue #126).

        Read-then-write, never an atomic swap: the fake models exactly what the
        real mutation does, so the accepted TOCTOU residual stays visible to
        tests instead of being fake-washed away. A body write bumps updatedAt
        (the `test_integration.py:102` discipline).
        """
        if self.update_body_error is not None:
            raise self.update_body_error
        stored = body
        if self.mangle_next_body_write:
            # One-shot: the stored bytes differ from the intended bytes.
            self.mangle_next_body_write = False
            stored = body + "\nMANGLED BY THE SERVER"
        self.body_writes.append((issue_id, body))
        bump = datetime.now(UTC)
        for issue in self._issues_with_id(issue_id):
            issue.description = stored
            issue.updated_at = bump

    async def add_issue_comment(self, issue_id, body):
        self.api_calls += 1
        if self.add_comment_error is not None:
            raise self.add_comment_error
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
        # issue #61 handoff surface + issue #43's PR node `id` (the marker
        # write target): [{"id", "number", "head_sha", "closes"}].
        self.api_calls += 1
        return [dict(pr) for pr in self.open_prs.get(head_ref, [])]

    async def fetch_pr_review_threads(self, pr_number):
        self.api_calls += 1
        return list(self.pr_review_threads.get(int(pr_number), []))

    async def fetch_pr_comments(self, pr_number):
        self.api_calls += 1
        return list(self.pr_comments.get(int(pr_number), []))

    async def set_sole_status_label(
            self, issue_id, label,
            expected_status=("status:in-progress", "status:todo")):
        self.api_calls += 1
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
                # Mirror of the real tracker: read back before compensating
                # (a failed removal is commit-ambiguous — PR #115 round 5).
                final = self._issues_with_id(issue_id)[0]
                status_after = sorted(
                    l for l in final.labels if l.startswith("status:"))
                if status_after == [label]:
                    if final.state.lower() == "closed":
                        self.remove_labels_error = None
                        await self.remove_labels(issue_id, [label])
                        raise TrackerError("handoff_preempted",
                                           "closed before ambiguous read-back")
                    return
                if added_now and label in after:
                    self.remove_labels_error = None
                    await self.remove_labels(issue_id, [label])
                raise
        final = self._issues_with_id(issue_id)[0]
        if final.state.lower() == "closed":
            # Mirror: closure wins mid-swap (PR #115 round 7).
            if label in final.labels:
                await self.remove_labels(issue_id, [label])
            raise TrackerError("handoff_preempted", "closed during swap")
        all_after = final.labels
        after = sorted(l for l in all_after if l.startswith("status:"))
        if after != [label]:
            # Mirror of the real tracker: a concurrent transition wins.
            if label in all_after and len(after) > 1:
                await self.remove_labels(issue_id, [label])
                raise TrackerError("handoff_preempted", f"concurrent: {after}")
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
    assert orch.sessions_for_issue("node-4") == {IMPLEMENT_ROLE: 1}
    for gated in ("node-1", "node-2", "node-3"):
        assert orch.sessions_for_issue(gated) == {}
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
    assert orch.sessions_for_issue("node-1") == {}
    assert orch.sessions_for_issue("node-2") == {IMPLEMENT_ROLE: 1}
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
    await wait_for(                                                    # session 2
        lambda: orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2})
    await wait_for(lambda: "node-1" in orch.parked)  # cap 2 exhausted -> parked

    assert len(tracker.comments) == 1                 # exactly one notification
    assert tracker.comments[0][0] == "node-1"
    assert "parked" in tracker.comments[0][1].lower()
    # issue #35: the comment names WHICH budget ran out (the label set cannot —
    # there is exactly one park label).
    assert "implement budget exhausted (2/2 implement sessions)" \
        in tracker.comments[0][1]
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
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}
    assert len(tracker.comments) == 1
    assert len(tracker.labels_added) == 2             # not re-labelled past park

    # The parking comment bumped updatedAt (FakeTracker mimics GitHub); the
    # label — not updatedAt — is authoritative, so the issue STAYS parked.
    await orch._tick()
    assert "node-1" in orch.parked
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}
    assert len(tracker.comments) == 1

    # human removes the status:parked label -> unparked, counter reset, dispatchable
    unparked = make_issue(1)  # labels back to just ["status:todo"]
    tracker.candidates = [unparked]
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    assert "node-1" not in orch.parked
    # counter reset on unpark: the re-dispatch is a FRESH session 1, not a
    # continuation of the pre-park count (which would immediately re-park).
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 1}
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
    assert orch.sessions_for_issue("node-1") == {}        # no fresh cap granted
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
    await wait_for(                                                    # ran to cap
        lambda: orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2})
    for _ in range(4):                                # keep ticking; write keeps failing
        await orch._tick()
        await asyncio.sleep(0.02)

    # counter held at cap, NOT reset
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}
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
    assert orch.sessions_for_issue("node-2") == {}


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


# --- per-role session budgets (issue #35 / AgDR-033) --------------------------


class VerifierPassRunner:
    """Models the triage verifier routing a PASS, at the real verifier's fidelity.

    On its `pass_on_turn`-th turn it writes the verdict the way the real verifier
    does: BOTH `status:todo` and `gate:triage-passed` in ONE `add_labels` call,
    then `status:triage` removed. The marker is not optional set dressing — a
    PASS modelled without it is refused by the dispatch guard (`_missing_marker`,
    which runs BEFORE the cap gate), so the issue would never reach the cap and
    the test would pass on unfixed code. Holds on `hold_on_turn` so the
    downstream implementer session is observable mid-flight.
    """

    provider_id = "fake"

    def __init__(self, tracker, issue_id, pass_on_turn, hold_on_turn=None):
        self.turn_timeout_ms = 5000
        self.stall_timeout_ms = 0
        self.max_budget_usd: float | None = None
        self.tracker = tracker
        self.issue_id = issue_id
        self.pass_on_turn = pass_on_turn
        self.hold_on_turn = hold_on_turn
        self.release = asyncio.Event()
        self.turns: list[tuple[str, str | None, str]] = []

    async def run_turn(self, workspace, prompt, resume_session_id, on_event,
                       issue_id, agent_token=None):
        n = len(self.turns) + 1
        self.turns.append((issue_id, resume_session_id, prompt))
        if n == self.pass_on_turn:
            await self.tracker.add_labels(
                self.issue_id, ["status:todo", "gate:triage-passed"])
            await self.tracker.remove_labels(self.issue_id, ["status:triage"])
        if n == self.hold_on_turn:
            await self.release.wait()
        return TurnResult(status="succeeded", session_id=f"sess-{n}",
                          cost_usd=0.01,
                          usage={"input_tokens": 1, "output_tokens": 1},
                          num_turns=1)


async def test_verify_spend_leaves_implementer_budget_intact(tmp_path, monkeypatch):
    """AC (issue #35): verifier and implementer sessions are capped SEPARATELY.

    A ticket that burns its FULL verify budget at `status:triage` must still get
    a full implementer budget once triage PASSes. On the role-agnostic counter
    the third dispatch found spent == cap and parked the ticket the moment it
    became implementable (live: #30, #15, #57).
    """
    tmpl = WORKFLOW_TMPL.replace(
        'active_states: ["todo", "in progress"]',
        'active_states: ["triage", "todo", "in progress"]')
    runner = VerifierPassRunner(None, "node-1", pass_on_turn=2, hold_on_turn=3)
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, tmpl, runner=runner)
    runner.tracker = tracker
    issue = make_issue(1, "triage")
    # ONE Issue object on both fetch paths: the verifier's label write is visible
    # to the worker's between-turn refresh AND the next poll, as on real GitHub.
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()                       # verify session 1 (max_turns: 1)
    # verify session 2 routes PASS mid-turn; the role pin ends it at the turn
    # boundary and the continuation re-dispatches into the implementer role.
    await wait_for(lambda: len(runner.turns) == 3)

    # POSITIVE discriminator, not absence-of-park: the issue is RUNNING, and the
    # implementer counter is a fresh 1 while the verify spend stands at its cap.
    assert "node-1" in orch.running
    assert orch.sessions_for_issue("node-1") == {VERIFY_ROLE: 2, IMPLEMENT_ROLE: 1}
    assert issue.labels == ["status:todo", "gate:triage-passed"]
    assert tracker.comments == []            # never parked, never refused
    assert "node-1" not in orch.parked

    runner.release.set()
    tracker.candidates = []                  # quiesce the continuation retry
    await wait_for(lambda: not orch.running)


async def test_verify_budget_exhaustion_park_comment_names_the_budget(
        tmp_path, monkeypatch):
    """AC (issue #35): there is exactly one park label, so the park COMMENT is
    the only place a reader can learn which budget ran out."""
    tmpl = WORKFLOW_TMPL.replace(
        'active_states: ["todo", "in progress"]',
        'active_states: ["triage", "todo", "in progress"]')
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    issue = make_issue(1, "triage")          # verifier never routes a verdict
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._tick()
    await wait_for(lambda: "node-1" in orch.parked)

    assert orch.sessions_for_issue("node-1") == {VERIFY_ROLE: 2}
    assert len(tracker.comments) == 1
    assert "verify budget exhausted (2/2 verify sessions)" in tracker.comments[0][1]
    # Label set unchanged: the sole park label, no new status:* vocabulary.
    assert tracker.labels_added == [("node-1", ("status:parked",))]


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
    assert orch.sessions_for_issue(issue.id) == {}
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


async def test_logged_out_claude_turn_refunds_session_and_never_records_success(
    tmp_path,
    monkeypatch,
):
    """Issue #116, the end of the chain the runner starts.

    `test_runner.py` replays the real logged-out capture and proves the runner
    now returns failed + PROVIDER_AUTHENTICATION; this is what that verdict then
    does to the scheduler. Pre-#116 the same run returned `succeeded`, so the
    worker exited with no exception and `_on_worker_done` fed `record_success()`
    — the logged-out CLI RESET the provider circuit while spending one session
    per dispatch until the issue false-parked at the cap (#112/#114).
    """
    runner = FakeRunner()
    runner.provider_id = "claude"

    async def logged_out_turn(*args, **kwargs):
        return TurnResult(
            status="failed",
            session_id="7c08bd33-23f4-426e-985b-218140f37abc",
            error="api_error",
            failure_class=FailureClass.PROVIDER_AUTHENTICATION,
        )

    runner.run_turn = logged_out_turn
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch, runner=runner)
    circuit = orch._provider_circuit("claude")
    successes = 0
    real_record_success = circuit.record_success

    def counting_record_success():
        nonlocal successes
        successes += 1
        return real_record_success()

    monkeypatch.setattr(circuit, "record_success", counting_record_success)
    issue = make_issue(1)
    tracker.candidates = [issue]
    tracker.states[issue.id] = issue
    sessions_before = dict(orch.sessions_per_issue)

    await orch._tick()
    await wait_for(lambda: issue.id in orch.provider_waiting)

    # the session spent on dispatch is refunded, so the cap is not burned
    assert dict(orch.sessions_per_issue) == sessions_before
    assert successes == 0                      # circuit never poisoned by a reset
    assert orch.provider_circuits["claude"].state is CircuitState.OPEN_LATCHED
    assert issue.id not in orch.retry_attempts
    assert issue.id not in orch.parked


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
    assert set(orch.sessions_per_issue) == {
        (issues[0].id, IMPLEMENT_ROLE), (issues[1].id, IMPLEMENT_ROLE)}
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
    assert all(orch.sessions_for_issue(issue.id) == {} for issue in issues)


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
    assert orch.sessions_for_issue(issue.id) == {}
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
    todo_sessions = orch.sessions_for_issue("node-1")
    assert todo_sessions == {IMPLEMENT_ROLE: 1}
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
    assert orch2.sessions_for_issue("node-1") == todo_sessions      # PARITY
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

    # cap spent on failures
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 3}
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


# --- fold-signal sub-poll (issue #51 part a) ----------------------------------


async def test_fold_signal_surfaces_once_per_process_and_writes_nothing(
    tmp_path, monkeypatch, capfd
):
    """A 👍 on a verdict comment of a `status:decision` issue surfaces exactly
    once per process lifetime, with zero tracker writes.

    The issue is in a GATE state, so `fetch_candidate_issues` never returns it —
    the poller reaches it only through `fetch_open_issues_by_status`, and the
    fake filters client-side exactly as the real tracker does.
    """
    tmpl = WORKFLOW_TMPL.replace(
        "polling:", "fold:\n  operator_logins: [\"Colin-Prologue\"]\n\npolling:"
    )
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, workflow_tmpl=tmpl)
    # Valid credential shape: NONE of SB_APP_* plus the GITHUB_TOKEN fallback.
    # Setting only SB_APP_BOT_LOGIN makes the App set partial and
    # validate_dispatch aborts the tick before the fold poll runs (CI-caught).
    for var in ("SB_APP_ID", "SB_APP_INSTALLATION_ID", "SB_APP_PRIVATE_KEY_FILE",
                "SB_APP_BOT_LOGIN", "SB_APP_BOT_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    issue = make_issue(51, state="decision")
    tracker.candidates = [issue]
    tracker.issue_comments["51"] = [
        IssueComment(
            id="IC_verdict",
            body="## Triage verdict\nbody-sha1: " + "a" * 40 + "\n\nNEEDS DECISION",
            login="switchboard-agent[bot]",
            created_at=datetime(2026, 8, 1, tzinfo=UTC),
            reactions=(
                CommentReaction(
                    id="RE_1", content="THUMBS_UP", login="colin-prologue",
                    created_at=datetime(2026, 8, 1, 1, tzinfo=UTC),
                ),
            ),
        ),
    ]

    await orch._tick()
    err = capfd.readouterr().err
    assert "fold signal detected" in err
    assert "verdict_comment_id=IC_verdict" in err
    assert orch.fold_signals_seen == {"RE_1:1:IC_verdict"}  # decision-identity key (PR #129 r1)

    # A gate-state issue is never dispatched, and part (a) writes NOTHING.
    assert runner.turns == []
    assert (tracker.comments, tracker.labels_added, tracker.labels_removed) == ([], [], [])

    # Second poll (interval forced open): the same standing reaction must not
    # re-emit within one process lifetime.
    orch._fold_last_poll_at = None
    await orch._tick()
    assert "fold signal detected" not in capfd.readouterr().err
    assert orch.fold_signals_seen == {"RE_1:1:IC_verdict"}  # decision-identity key (PR #129 r1)


async def test_fold_poll_disabled_by_default_makes_no_calls(harness, monkeypatch):
    """Empty `fold.operator_logins` (the shipped default) = detection off."""
    orch, tracker, _, _ = harness

    async def boom(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("fold poll ran with an empty operator allowlist")

    tracker.fetch_open_issues_by_status = boom
    tracker.candidates = [make_issue(52, state="decision")]
    await orch._tick()
    assert orch.fold_signals_seen == set()


# --- fold-signal APPLY (issue #126 part b) ------------------------------------
#
# Detection is part (a); everything below exercises the WRITE half against the
# FakeTracker. The scenarios are organised around the one invariant that makes
# the loop safe: the fold-applied MARKER — not the body digest — is what says a
# fold happened, because a completed fold's after-digest survives an untouched
# re-triage round and therefore cannot distinguish "resume" from "done weeks
# ago".

ORIGINAL_BODY = "# Issue 126\n\nThe body the operator approved.\n"

# A whole-body replacement that quotes a ``` fence — the payload class the
# sentinel pair exists to survive.
REVISED_BODY = (
    "# Issue 126 (folded)\n"
    "\n"
    "## Mechanics\n"
    "\n"
    "```python\n"
    "assert body_digest(payload) == after_sha1\n"
    "```\n"
)

OPERATOR = "colin-prologue"


def _fold_harness(tmp_path, monkeypatch, *, state="drafting", body=ORIGINAL_BODY):
    """An orchestrator whose fold poll is armed, plus one gate-state issue.

    Credential shape matters (PR #129): setting only SB_APP_BOT_LOGIN makes the
    App set partial and validate_dispatch aborts the tick before the fold poll.
    """
    tmpl = WORKFLOW_TMPL.replace(
        "polling:", 'fold:\n  operator_logins: ["Colin-Prologue"]\n\npolling:')
    orch, tracker, _runner, _ = _build_harness(
        tmp_path, monkeypatch, workflow_tmpl=tmpl)
    for var in ("SB_APP_ID", "SB_APP_INSTALLATION_ID", "SB_APP_PRIVATE_KEY_FILE",
                "SB_APP_BOT_LOGIN", "SB_APP_BOT_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    issue = make_issue(126, state=state)
    issue.description = body
    tracker.candidates = [issue]
    tracker.states = {issue.id: issue}
    return orch, tracker, issue


def _verdict(cid, *, sha1, proposal=None, minute=1, thumbs_up=False):
    parts = ["## Triage verdict", f"body-sha1: {sha1}", "",
             "NEEDS WORK — findings above."]
    if proposal is not None:
        parts += ["", PROPOSAL_OPEN, proposal, PROPOSAL_CLOSE]
    reactions = ()
    if thumbs_up:
        reactions = (CommentReaction(
            id=f"RE_{cid}", content="THUMBS_UP", login=OPERATOR,
            created_at=datetime(2026, 8, 1, 12, minute, tzinfo=UTC)),)
    return IssueComment(
        id=cid, body="\n".join(parts), login="switchboard-agent[bot]",
        created_at=datetime(2026, 8, 1, 12, minute, tzinfo=UTC),
        reactions=reactions,
    )


def _vetoed(verdict):
    return dc_replace(verdict, reactions=(CommentReaction(
        id="RE_veto", content="THUMBS_DOWN", login=OPERATOR,
        created_at=datetime(2026, 8, 1, 13, tzinfo=UTC)),))


def _operator_comment(cid, text, *, minute=5):
    return IssueComment(
        id=cid, body=text, login=OPERATOR,
        created_at=datetime(2026, 8, 1, 12, minute, tzinfo=UTC))


async def _fold_poll(orch, tracker):
    """One fold sub-poll with the cadence gate forced open.

    Called directly rather than through `_tick` so the `states_calls` ordinal
    seam means exactly what Mechanics 12 says it means — the TEST owns the
    per-invocation boundary, and a dispatch pass in the same tick would shift
    the ordinals under it.
    """
    orch._fold_last_poll_at = None
    return await orch._poll_fold_signals(tracker)


def _marker_comments(tracker):
    return [body for _id, body in tracker.comments
            if body.startswith(FOLD_MARKER_PREFIX)]


def _status_labels(issue):
    return sorted(lbl for lbl in issue.labels if lbl.startswith("status:"))


# --- the happy path + the relabel contract (AC 1, AC 2 provenance) ------------


async def test_approved_fold_writes_body_then_marker_then_relabels(
        tmp_path, monkeypatch):
    """Write order body -> marker -> relabel, and the marker's recorded values.

    `before:` is the digest the OPERATOR approved (signal.body_sha1); `after:`
    is a FRESH Step-0 computation over the stored body — asserted, not stated.
    """
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True)]

    # The relabel must go through the cited helper with the drafting-only
    # expected set — the helper's DEFAULT set would refuse a drafting issue.
    seen = {}
    real_swap = tracker.set_sole_status_label

    async def spy(issue_id, label, expected_status=None):
        seen["args"] = (issue_id, label, expected_status)
        return await real_swap(issue_id, label, expected_status=expected_status)

    tracker.set_sole_status_label = spy

    await _fold_poll(orch, tracker)

    assert tracker.body_writes == [(issue.id, REVISED_BODY)]
    assert issue.description == REVISED_BODY
    assert seen["args"] == (issue.id, "status:triage", ("status:drafting",))
    assert _status_labels(issue) == ["status:triage"]

    markers = _marker_comments(tracker)
    assert len(markers) == 1
    after = body_digest(issue.description)   # fresh Step-0 over the STORED bytes
    assert markers[0].splitlines()[0] == (
        f"<!-- switchboard:fold-applied verdict:IC_v "
        f"before:{before} after:{after} -->")
    assert after == body_digest(REVISED_BODY)
    assert before != after


# --- CAS refusal on a true mismatch (AC 1) ------------------------------------


async def test_body_changed_under_the_fold_is_refused_and_consumed(
        tmp_path, monkeypatch, capfd):
    """The fake recomputes the digest from the MUTATED body (read-then-write,
    never an atomic swap), so this is a real mismatch, not a staged one."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]
    # A third party edited the body between approval and apply.
    issue.description = ORIGINAL_BODY + "\nsomeone else edited this.\n"

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err
    assert "status=refused_clobber" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments,
            tracker.sole_status_swaps) == ([], [], [])
    assert _status_labels(issue) == ["status:drafting"]

    # Consumed: a decided outcome, so the next poll must not re-emit.
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}
    await _fold_poll(orch, tracker)
    assert "fold signal detected" not in capfd.readouterr().err
    assert tracker.body_writes == []


# --- verify-after-write divergence (AC 1) -------------------------------------


async def test_verify_after_write_divergence_is_reported_not_claimed(
        tmp_path, monkeypatch, capfd):
    """Induced with the one-shot `mangle_next_body_write`: the fake stays
    derive-faithful everywhere else, so this is the only honest way to reach the
    branch. No marker, no relabel — and CONSUMED, because a re-emission's digest
    would match neither before- nor after-sha1 and would just refuse as a
    clobber one cycle later."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]
    tracker.mangle_next_body_write = True

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "verify-after-write DIVERGENCE" in err
    assert len(tracker.body_writes) == 1
    assert _marker_comments(tracker) == []                # marker ABSENT
    assert tracker.comments == []
    assert _status_labels(issue) == ["status:drafting"]   # label unchanged
    assert tracker.sole_status_swaps == []
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}   # consumed


# --- partial-fold states (AC 2) -----------------------------------------------


async def test_marker_failure_after_body_write_resumes_on_the_next_poll(
        tmp_path, monkeypatch):
    """(i) The body landed, the marker did not. The re-entry rule reads the
    current digest as after-sha1 and RESUMES marker+relabel — no second body
    write. `before:` is asserted against `signal.body_sha1`, which in a resume
    is the only source left: the pre-fold body no longer exists to re-read."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True)]
    tracker.add_comment_error = TrackerError("github_api_status", "comment 502")

    await _fold_poll(orch, tracker)
    assert len(tracker.body_writes) == 1
    assert tracker.comments == []
    assert _status_labels(issue) == ["status:drafting"]
    assert orch.fold_signals_seen == set()          # re-emittable, not consumed

    tracker.add_comment_error = None
    await _fold_poll(orch, tracker)

    assert len(tracker.body_writes) == 1            # body NOT rewritten
    markers = _marker_comments(tracker)
    assert len(markers) == 1
    after = body_digest(issue.description)
    assert markers[0].splitlines()[0] == (
        f"<!-- switchboard:fold-applied verdict:IC_v "
        f"before:{before} after:{after} -->")
    assert _status_labels(issue) == ["status:triage"]
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


async def test_relabel_failure_is_terminal_and_logged_not_resumed(
        tmp_path, monkeypatch, capfd):
    """(ii) Round-6 decision (b): the marker means COMPLETE-OR-TERMINAL. Body
    and marker are durable, the issue stays at drafting for a hand relabel, and
    a post-restart re-emission hits marker-first and writes nothing."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]
    tracker.add_labels_error = TrackerError("github_api_status", "label 502")

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "RELABEL FAILED after body+marker" in err
    assert len(tracker.body_writes) == 1
    marker_body = _marker_comments(tracker)[0]
    assert _status_labels(issue) == ["status:drafting"]   # label unchanged
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}   # consumed, not retried

    # Restart: the per-process dedupe set is gone; the DURABLE marker is the
    # real cross-restart dedupe, so it must be on the thread apply re-reads.
    tracker.issue_comments["126"] = tracker.issue_comments["126"] + [
        IssueComment(id="IC_marker", body=marker_body,
                     login="switchboard-agent[bot]",
                     created_at=datetime(2026, 8, 1, 13, tzinfo=UTC))]
    tracker.add_labels_error = None
    orch.fold_signals_seen.clear()
    await _fold_poll(orch, tracker)

    assert len(tracker.body_writes) == 1
    assert len(tracker.comments) == 1
    assert _status_labels(issue) == ["status:drafting"]   # still not relabelled
    assert "status=already_folded" in capfd.readouterr().err


async def test_completed_fold_re_emitted_after_a_re_triage_round_writes_nothing(
        tmp_path, monkeypatch, capfd):
    """(iii) fold -> re-triage NEEDS WORK -> relabel back to drafting leaves the
    body digest at after-sha1, so digests alone cannot tell "resume a partial
    fold" from "done weeks ago". Marker-first is what can."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True)]

    await _fold_poll(orch, tracker)
    assert _status_labels(issue) == ["status:triage"]
    marker_body = _marker_comments(tracker)[0]
    after = body_digest(issue.description)
    assert marker_body.splitlines()[0] == (
        f"<!-- switchboard:fold-applied verdict:IC_v "
        f"before:{before} after:{after} -->")

    # The marker comment is now part of the thread the next poll re-reads.
    tracker.issue_comments["126"] = tracker.issue_comments["126"] + [
        IssueComment(id="IC_marker", body=marker_body,
                     login="switchboard-agent[bot]",
                     created_at=datetime(2026, 8, 1, 13, tzinfo=UTC))]
    # A later re-triage round routed NEEDS WORK and put it back at drafting with
    # the body untouched — so the digest still equals after-sha1. The re-triage
    # POSTS ITS VERDICT (newer than the marker): that newer verdict is what
    # discriminates this quiet case from a STRANDED fold (codex review, PR #132).
    tracker.issue_comments["126"] = tracker.issue_comments["126"] + [
        IssueComment(id="IC_v2",
                     body=f"## Triage verdict\nbody-sha1: {after}\n\n"
                          "NEEDS WORK — round 2 findings.",
                     login="switchboard-agent[bot]",
                     created_at=datetime(2026, 8, 1, 14, tzinfo=UTC))]
    issue.labels = ["status:drafting"]
    _recompute_state_from_labels(issue)
    orch.fold_signals_seen.clear()          # restart
    baseline = (len(tracker.body_writes), len(tracker.comments))

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert (len(tracker.body_writes), len(tracker.comments)) == baseline
    assert _status_labels(issue) == ["status:drafting"]   # NOT re-relabelled
    assert "status=already_folded" in err
    assert "STRANDED" not in err            # the quiet case stays quiet


async def test_stranded_fold_marker_present_no_newer_verdict_is_loud(
        tmp_path, monkeypatch, capfd):
    """Codex review (PR #132): a commit-ambiguous marker post (`addComment`
    committed, response lost) leaves body+marker durable and the relabel
    never-run. The next poll's marker-first must not consume that SILENTLY:
    marker present + still drafting + NO verdict newer than the marker ==
    stranded, and the diagnostic matches the relabel-failure one. Still zero
    writes and no relabel (round-6 decision (b))."""
    orch, tracker, issue = _fold_harness(
        tmp_path, monkeypatch, body=REVISED_BODY)   # body already folded
    before = body_digest(ORIGINAL_BODY)
    after = body_digest(REVISED_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True),
        IssueComment(id="IC_marker",
                     body=marker_first_line("IC_v", before, after) + "\n\nprov.",
                     login="switchboard-agent[bot]",
                     created_at=datetime(2026, 8, 1, 13, tzinfo=UTC)),
    ]
    baseline = (len(tracker.body_writes), len(tracker.comments))

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "STRANDED FOLD" in err
    assert "status=already_folded_stranded" in err
    assert (len(tracker.body_writes), len(tracker.comments)) == baseline
    assert _status_labels(issue) == ["status:drafting"]   # no relabel (b)
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}   # consumed


async def test_human_transition_between_poll_and_apply_wins(
        tmp_path, monkeypatch, capfd):
    """Codex review (PR #132): the poll snapshot said drafting, but a human
    moved the issue before apply's fresh read. Only closure was checked; a
    stale approval must never rewrite the body after a newer human transition.
    The fresh read's labels gate the body write."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True)]

    real_fetch = tracker.fetch_issue_states_by_ids

    async def flip_then_fetch(ids):
        # The sharper case (codex re-review): a MID-SWAP dual-label state.
        # Membership on status:drafting would pass; only the sole-status
        # check (mirroring set_sole_status_label's preemption semantics)
        # refuses it before the body write.
        issue.labels = ["status:drafting", "status:decision"]
        _recompute_state_from_labels(issue)
        return await real_fetch(ids)

    monkeypatch.setattr(tracker, "fetch_issue_states_by_ids", flip_then_fetch)

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_state_changed" in err
    assert tracker.body_writes == []                      # body NOT rewritten
    assert _marker_comments(tracker) == []
    assert sorted(issue.labels) == ["status:decision", "status:drafting"]  # untouched
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}   # consumed


async def test_marker_from_non_bot_author_cannot_suppress_the_fold(
        tmp_path, monkeypatch):
    """Codex review (PR #132): with the App identity configured, marker
    recognition requires the bot author — any other participant posting the
    deterministic prefix must not consume a legitimate approved fold as
    already_folded."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    spoof = marker_first_line("IC_v", before, body_digest(REVISED_BODY))
    comments = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True),
        IssueComment(id="IC_spoof", body=spoof + "\n",
                     login="mallory",
                     created_at=datetime(2026, 8, 1, 13, tzinfo=UTC)),
    ]
    tracker.issue_comments["126"] = comments
    signal = FoldSignal(
        issue_id=issue.id, issue_identifier="126", verdict_comment_id="IC_v",
        body_sha1=before, channel="reaction", approved=True,
        operator_login=OPERATOR, source_node_id="RE_IC_v",
    )

    outcome = await apply_fold_signal(
        tracker, signal, issue=issue, bot_login="switchboard-agent[bot]")
    assert outcome.status == "applied"
    assert tracker.body_writes == [(issue.id, REVISED_BODY)]

    # Unconfigured (GITHUB_TOKEN mode) the author is not checked — the
    # single-operator premise — so the same spoof would suppress; that
    # behaviour is exercised by the marker tests above via login-bearing
    # markers accepted with bot_login=None.


async def test_a_comment_quoting_the_marker_does_not_suppress_the_fold(
        tmp_path, monkeypatch):
    """Round-10 negative case: marker-first is a FIRST-LINE prefix match, never
    a substring scan. Verdicts embed whole revised bodies and bodies quote
    sentinels, so a substring scan would silently cancel real folds."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    quoted = marker_first_line("IC_v", before, body_digest(REVISED_BODY))
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True),
        _operator_comment("IC_chat",
                          f"For the record the marker will read:\n\n{quoted}\n"),
    ]

    await _fold_poll(orch, tracker)

    assert tracker.body_writes == [(issue.id, REVISED_BODY)]
    assert len(_marker_comments(tracker)) == 1
    assert _status_labels(issue) == ["status:triage"]


# --- re-read revalidation refusal (AC 3) --------------------------------------


async def test_verdict_edited_between_detection_and_apply_is_refused_and_consumed(
        tmp_path, monkeypatch, capfd):
    """A retargeted verdict is a DIFFERENT fold. An edit changes no
    `dedupe_key()` field, so no superseding signal ever arrives — an unconsumed
    refusal would re-emit every poll forever. That is why this consumes."""
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=before, proposal=REVISED_BODY, thumbs_up=True)]

    # Detection reads the thread once; apply re-reads it. The operator edited
    # the verdict in between.
    real_fetch = tracker.fetch_issue_comments
    calls = {"n": 0}

    async def edited(issue_number):
        calls["n"] += 1
        thread = await real_fetch(issue_number)
        if calls["n"] == 1:
            return thread
        return [dc_replace(c, body=c.body.replace(before, "b" * 40))
                for c in thread]

    tracker.fetch_issue_comments = edited

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=refused_verdict_edited" in err
    assert (tracker.body_writes, tracker.comments,
            tracker.sole_status_swaps) == ([], [], [])
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}

    await _fold_poll(orch, tracker)
    assert "fold signal detected" not in capfd.readouterr().err


# --- vetoes (AC 4) ------------------------------------------------------------


async def test_thumbs_down_veto_writes_nothing_and_is_consumed(
        tmp_path, monkeypatch, capfd):
    """A veto is in the signal stream BY DESIGN so the operator can see it was
    seen and honoured. Apply's job is to fold nothing and consume it."""
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [_vetoed(
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY), proposal=REVISED_BODY))]

    await _fold_poll(orch, tracker)

    assert (tracker.body_writes, tracker.comments,
            tracker.sole_status_swaps) == ([], [], [])
    assert "status=vetoed" in capfd.readouterr().err
    assert orch.fold_signals_seen == {"RE_veto:0:IC_v"}
    await _fold_poll(orch, tracker)
    assert "fold signal detected" not in capfd.readouterr().err


async def test_no_fold_command_writes_nothing_and_is_consumed(
        tmp_path, monkeypatch, capfd):
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY), proposal=REVISED_BODY),
        _operator_comment("IC_cmd", "/no-fold — I want to reword this first."),
    ]

    await _fold_poll(orch, tracker)

    assert (tracker.body_writes, tracker.comments,
            tracker.sole_status_swaps) == ([], [], [])
    assert "status=vetoed" in capfd.readouterr().err
    assert orch.fold_signals_seen == {"IC_cmd:0:IC_v"}
    await _fold_poll(orch, tracker)
    assert "fold signal detected" not in capfd.readouterr().err


# --- the diagnosed skips (AC 5) -----------------------------------------------


async def test_decision_state_signal_logs_and_skips(tmp_path, monkeypatch, capfd):
    """`decision -> triage` is deliberately illegal, and a NEEDS-DECISION verdict
    predates the operator's answer so it carries no proposal."""
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch, state="decision")
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY), thumbs_up=True)]

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_decision_state" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments,
            tracker.sole_status_swaps) == ([], [], [])
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


async def test_retrofit_verdict_without_body_sha1_logs_and_skips(
        tmp_path, monkeypatch, capfd):
    """Every pre-#55 verdict: no `body-sha1:` anywhere, so there is no
    before-digest to compare against."""
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [IssueComment(
        id="IC_v",
        body="## Triage verdict\n\nNEEDS WORK (pre-#55, no digest line).",
        login="switchboard-agent[bot]",
        created_at=datetime(2026, 8, 1, 12, tzinfo=UTC),
        reactions=(CommentReaction(
            id="RE_1", content="THUMBS_UP", login=OPERATOR,
            created_at=datetime(2026, 8, 1, 13, tzinfo=UTC)),),
    )]

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_retrofit" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments) == ([], [])
    assert orch.fold_signals_seen == {"RE_1:1:IC_v"}


async def test_fallback_exhausted_logs_and_skips(tmp_path, monkeypatch, capfd):
    """A proposal-less verdict with no same-digest proposal-bearing verdict to
    bind forward to."""
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY), thumbs_up=True)]

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_no_proposal" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments) == ([], [])
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


async def test_issue_closed_before_the_body_write_logs_and_skips(
        tmp_path, monkeypatch, capfd):
    """The relabel helper's own closed guard fires too late to prevent a partial
    fold, so apply re-checks before the BODY write."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]
    # Closed between the poll's gate-state fetch and apply's guard read.
    tracker.states = {issue.id: dc_replace(issue, state="closed")}

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_closed" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments) == ([], [])
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


async def test_stale_verdict_comment_logs_and_skips(tmp_path, monkeypatch, capfd):
    """The approved verdict was deleted between detection and apply."""
    orch, tracker, _issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]

    real_fetch = tracker.fetch_issue_comments
    calls = {"n": 0}

    async def deleted(issue_number):
        calls["n"] += 1
        thread = await real_fetch(issue_number)
        return thread if calls["n"] == 1 else []

    tracker.fetch_issue_comments = deleted

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_stale" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments) == ([], [])
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


async def test_malformed_proposal_logs_and_skips_never_a_partial_apply(
        tmp_path, monkeypatch, capfd):
    """A SECOND close literal inside the payload. The naive non-greedy read would
    apply a body truncated at the first one, green on every downstream check —
    verify-after-write compares stored bytes to INTENDED bytes and is
    structurally blind to it."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=f"real body\n{PROPOSAL_CLOSE}\nthe rest of the body",
                 thumbs_up=True)]

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert "status=skipped_malformed_proposal" in err
    assert "bound_comment_id=IC_v" in err
    assert (tracker.body_writes, tracker.comments) == ([], [])
    assert issue.description == ORIGINAL_BODY
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


# --- binding across several same-digest verdicts (AC 6) -----------------------


async def test_referral_binds_to_the_latest_same_digest_verdict_with_a_proposal(
        tmp_path, monkeypatch):
    """A fast-path referral carries the digest but no block, so it binds forward.
    Tiebreak is the house latest-wins rule (`fold.py:181-186`)."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        _verdict("IC_old", sha1=before, proposal="# stale proposal\n", minute=1),
        _verdict("IC_new", sha1=before, proposal=REVISED_BODY, minute=2),
        _verdict("IC_ref", sha1=before, minute=3, thumbs_up=True),  # referral
    ]

    await _fold_poll(orch, tracker)

    assert tracker.body_writes == [(issue.id, REVISED_BODY)]   # not the stale one
    markers = _marker_comments(tracker)
    assert len(markers) == 1
    # Keyed on the BOUND comment, not the approved one.
    assert markers[0].splitlines()[0].startswith(
        "<!-- switchboard:fold-applied verdict:IC_new ")


async def test_two_routes_to_one_proposal_in_one_batch_fold_exactly_once(
        tmp_path, monkeypatch, capfd):
    """Round 8: keying the marker on the SIGNAL's verdict id would give these two
    signals different keys, so the second would slip past marker-first and resume
    into a spurious relabel. Keying on the BOUND comment is what makes the second
    a zero-write consume — and apply's PER-SIGNAL thread re-read (never one
    pre-batch snapshot) is what lets it see the marker the first one just
    posted."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    before = body_digest(ORIGINAL_BODY)
    tracker.issue_comments["126"] = [
        # Route 1: the proposal-bearing verdict, approved directly.
        _verdict("IC_new", sha1=before, proposal=REVISED_BODY, minute=2,
                 thumbs_up=True),
        # Route 2: a referral approved separately, binding back to IC_new.
        _verdict("IC_ref", sha1=before, minute=3, thumbs_up=True),
    ]

    # GitHub publishes a posted comment to the thread; the fake's comment log is
    # write-only, so mirror the publish or the second signal's re-read would miss
    # the marker the first one wrote.
    real_add = tracker.add_issue_comment

    async def add_and_publish(issue_id, body):
        await real_add(issue_id, body)
        tracker.issue_comments["126"] = tracker.issue_comments["126"] + [
            IssueComment(id=f"IC_m{len(tracker.comments)}", body=body,
                         login="switchboard-agent[bot]",
                         created_at=datetime(2026, 8, 1, 14, tzinfo=UTC))]

    tracker.add_issue_comment = add_and_publish

    await _fold_poll(orch, tracker)
    err = capfd.readouterr().err

    assert tracker.body_writes == [(issue.id, REVISED_BODY)]   # exactly one
    assert len(_marker_comments(tracker)) == 1
    assert tracker.sole_status_swaps == [(issue.id, "status:triage")]
    assert "status=already_folded" in err
    # Both signals reached a decided outcome. Their dedupe keys are SIGNAL-
    # scoped (`verdict_comment_id`) and therefore differ — which is precisely
    # why the marker key cannot be: two keys, one fold.
    assert orch.fold_signals_seen == {"RE_IC_new:1:IC_new", "RE_IC_ref:1:IC_ref"}


# --- transient failures leave the signal re-emittable (AC 8) ------------------


async def test_transient_body_write_error_leaves_the_signal_re_emittable(
        tmp_path, monkeypatch):
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]
    tracker.update_body_error = TrackerError("github_api_status", "body 502")

    await _fold_poll(orch, tracker)
    assert (tracker.body_writes, tracker.comments) == ([], [])
    assert orch.fold_signals_seen == set()

    tracker.update_body_error = None
    await _fold_poll(orch, tracker)

    assert tracker.body_writes == [(issue.id, REVISED_BODY)]
    assert len(_marker_comments(tracker)) == 1
    assert _status_labels(issue) == ["status:triage"]
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}


@pytest.mark.parametrize("seam", ["thread_reread", "guard_read", "verify_read"])
async def test_tracker_error_on_any_of_applys_three_reads_is_re_emittable(
        tmp_path, monkeypatch, seam):
    """All three reads are safe to retry: the thread re-read and the guard read
    precede every write, and after a failed verify re-read the digest is either
    before-sha1 (nothing written) or after-sha1 (Mechanics 7 resumes)."""
    orch, tracker, issue = _fold_harness(tmp_path, monkeypatch)
    tracker.issue_comments["126"] = [
        _verdict("IC_v", sha1=body_digest(ORIGINAL_BODY),
                 proposal=REVISED_BODY, thumbs_up=True)]
    real_fetch = tracker.fetch_issue_comments

    if seam == "thread_reread":
        calls = {"n": 0}

        async def flaky(issue_number):
            calls["n"] += 1
            if calls["n"] == 2:   # detection reads first, apply's re-read second
                raise TrackerError("github_api_status", "comments 502")
            return await real_fetch(issue_number)

        tracker.fetch_issue_comments = flaky
    else:
        # Ordinal scope is per APPLY INVOCATION and the TEST owns the boundary:
        # reset the counter immediately before the poll being armed. Ordinal 1 is
        # the guard + before-digest read, ordinal 2 the verify-after-write
        # re-read (ordinal 2 is DEFINED only for an invocation that reaches the
        # body write, which this one does).
        tracker.states_calls = 0
        tracker.states_error_at_ordinal = 1 if seam == "guard_read" else 2

    await _fold_poll(orch, tracker)

    assert orch.fold_signals_seen == set()               # re-emittable
    assert tracker.comments == []                        # no marker
    assert _status_labels(issue) == ["status:drafting"]
    if seam == "verify_read":
        assert len(tracker.body_writes) == 1             # the write landed
    else:
        assert tracker.body_writes == []

    # Once the transient clears, the same signal completes the fold — and the
    # body is never written twice.
    tracker.states_error_at_ordinal = None
    tracker.fetch_issue_comments = real_fetch
    await _fold_poll(orch, tracker)

    assert len(tracker.body_writes) == 1
    assert len(_marker_comments(tracker)) == 1
    assert _status_labels(issue) == ["status:triage"]
    assert orch.fold_signals_seen == {"RE_IC_v:1:IC_v"}

# --- review-response sub-poll (issue #43 / AgDR-037) --------------------------
#
# The ENABLING config is BOTH conditions: a non-empty `review_response.bot_logins`
# AND a set `SB_APP_BOT_LOGIN`. Either missing disables the loop, so every test
# below that expects a trigger states both explicitly.

RR_BOT = "codex-bot"
RR_SELF = "switchboard-agent"
RR_TMPL = WORKFLOW_TMPL.replace(
    "polling:", f"review_response:\n  bot_logins: [\"{RR_BOT}\"]\n\npolling:"
)


def _enable_app_identity(monkeypatch, login: str | None = RR_SELF):
    """The App credential set, or the dogfood path that lacks it.

    The set must be COMPLETE or `validate_dispatch` refuses the whole tick (a
    partial set is an unnoticed identity switch, PR #42 P2). So "SB_APP_BOT_LOGIN
    unset" is NOT a partial App set — it is reachable exactly one way, the
    documented GITHUB_TOKEN dogfood path, where all five are unset and worker
    replies are authored by the OPERATOR's login. That is what `login=None`
    models.
    """
    if login is None:
        for var in ("SB_APP_ID", "SB_APP_INSTALLATION_ID",
                    "SB_APP_PRIVATE_KEY_FILE", "SB_APP_BOT_LOGIN",
                    "SB_APP_BOT_USER_ID"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        return
    for var, value in (
        ("SB_APP_ID", "1"), ("SB_APP_INSTALLATION_ID", "2"),
        ("SB_APP_PRIVATE_KEY_FILE", "/dev/null"), ("SB_APP_BOT_USER_ID", "3"),
        ("SB_APP_BOT_LOGIN", login),
    ):
        monkeypatch.setenv(var, value)


def rr_thread(*logins_and_minutes, resolved=False, id_="RT_1") -> ReviewThread:
    """A review thread carrying ONLY the fidelity set the predicate reads:
    `isResolved`, per-comment author login, comment `createdAt`. No verdict is
    stored — `needs_response` derives it, exactly as it does from the server's
    payload (OBS-023)."""
    base = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    return ReviewThread(
        id=id_,
        is_resolved=resolved,
        comments=tuple(
            ReviewThreadComment(
                id=f"{id_}_C{i}", login=login,
                created_at=base + timedelta(minutes=minutes),
            )
            for i, (login, minutes) in enumerate(logins_and_minutes)
        ),
    )


def rr_issue(n: int) -> Issue:
    """A `status:human-review` issue as one REALLY looks at Gate C.

    It carries `gate:triage-passed`: the issue reached human-review through
    `todo`, and that marker deliberately survives — which is exactly why the
    relabeled todo is claimable without the sub-poll stamping a marker of its
    own. `make_issue` only stamps it for `state="todo"`, so a test that dropped
    it would be asserting against an issue the pipeline cannot produce, and
    would hide the dispatch guard refusing the relabeled issue.
    """
    issue = make_issue(n, state="human review")
    issue.labels.append("gate:triage-passed")
    return issue


def _bind_pr(tracker, issue_number: int, pr_number: int = 500, closes=None):
    tracker.open_prs[f"switchboard/issue-{issue_number}"] = [{
        "id": f"PR_node_{pr_number}", "number": pr_number, "head_sha": "a" * 40,
        "closes": [issue_number] if closes is None else closes,
    }]
    return pr_number


def _rr_harness(tmp_path, monkeypatch, *, login=RR_SELF, tmpl=RR_TMPL):
    _enable_app_identity(monkeypatch, login)
    return _build_harness(tmp_path, monkeypatch, workflow_tmpl=tmpl)


async def test_review_trigger_relabels_and_writes_a_full_round_marker(
    tmp_path, monkeypatch, capfd
):
    """AC 1: a human-review issue whose bound PR owes a bot reply is relabeled
    to todo within one poll cycle, with the round marker's CONTENT asserted.

    Presence-only would let a short-form marker ship green and silently defeat
    the prompt addendum's guard, which reads `bots=` and `self=` off this line.
    """
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    issue = rr_issue(43)
    tracker.candidates = [issue]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]

    assert await orch._poll_review_responses(tracker) == ["43"]

    # The marker is written BEFORE the relabel: a crash between them burns a
    # round harmlessly, while the reverse order hands out an unaccounted round.
    assert tracker.comments == [(
        f"PR_node_{pr}",
        f"<!-- switchboard:response-round n=1 bots={RR_BOT} self={RR_SELF} -->",
    )]
    assert tracker.sole_status_swaps == [(issue.id, TODO_LABEL)]
    assert issue.labels.count("status:todo") == 1
    assert "status:human-review" not in issue.labels
    assert "review-response triggered" in capfd.readouterr().err


async def test_review_trigger_marker_records_the_parsed_not_the_raw_config(
    tmp_path, monkeypatch
):
    """The marker records NORMALIZED values — lowercased, deduped, and the
    `[bot]` suffix stripped from the identity. A session in another process
    parses this line, so what lands must be what the predicate would match."""
    tmpl = WORKFLOW_TMPL.replace(
        "polling:",
        'review_response:\n  bot_logins: ["Codex-Bot", " codex-bot ", "Other"]\n\n'
        "polling:",
    )
    orch, tracker, _, _ = _rr_harness(
        tmp_path, monkeypatch, login="Switchboard-Agent[bot]", tmpl=tmpl,
    )
    tracker.candidates = [rr_issue(43)]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]

    await orch._poll_review_responses(tracker)
    assert tracker.comments[0][1] == (
        "<!-- switchboard:response-round n=1 bots=codex-bot,other "
        "self=switchboard-agent -->"
    )


async def test_review_poll_leaves_an_issue_with_nothing_owed_untouched(
    tmp_path, monkeypatch
):
    """AC 1, other half. Three settled shapes, none of which is a trigger:
    resolved, answered-by-Switchboard, and an unlisted author."""
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    issue = rr_issue(43)
    tracker.candidates = [issue]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [
        rr_thread((RR_BOT, 0), resolved=True, id_="RT_resolved"),
        rr_thread((RR_BOT, 0), (RR_SELF, 5), id_="RT_answered"),
        rr_thread(("some-human", 0), id_="RT_human"),
    ]

    assert await orch._poll_review_responses(tracker) == []
    assert (tracker.comments, tracker.sole_status_swaps) == ([], [])
    assert "status:human-review" in issue.labels


async def test_review_round_cap_stops_relabeling_and_comments_once(
    tmp_path, monkeypatch
):
    """AC 2: at the cap the orchestrator STOPS RELABELING and posts exactly one
    operator comment — it does not park (no active human-review -> parked edge
    exists, and `_park` clears only IN_PROGRESS_LABEL, stranding a dual label).

    The cap comment is guarded by its OWN marker: at the cap the round marker is
    present by construction and so cannot guard it.
    """
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    issue = rr_issue(43)
    tracker.candidates = [issue]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]
    tracker.pr_comments[pr] = [
        IssueComment(
            id="IC_1", login=RR_SELF,
            body=f"<!-- switchboard:response-round n=1 bots={RR_BOT} self={RR_SELF} -->",
        ),
        IssueComment(
            id="IC_2", login=RR_SELF,
            body=f"<!-- switchboard:response-round n=2 bots={RR_BOT} self={RR_SELF} -->",
        ),
    ]

    assert await orch._poll_review_responses(tracker) == []
    assert tracker.sole_status_swaps == []            # no further relabel
    assert "status:human-review" in issue.labels
    assert len(tracker.comments) == 1
    assert scheduler_mod.CAP_MARKER in tracker.comments[0][1]

    # Idempotent: the cap comment the poll just wrote is now on the PR, so a
    # later cycle must recognize its own guard marker and stay silent.
    tracker.pr_comments[pr].append(
        IssueComment(id="IC_cap", login=RR_SELF, body=tracker.comments[0][1])
    )
    orch._review_last_poll_at = None
    assert await orch._poll_review_responses(tracker) == []
    assert len(tracker.comments) == 1


async def test_review_round_markers_survive_an_orchestrator_restart(
    tmp_path, monkeypatch
):
    """AC 2, durability. The count is NOT process state: it is a comment on the
    PR, re-read every poll. A restarted orchestrator must not hand out a fresh
    budget of rounds — so a rebuilt scheduler over the SAME tracker still caps.
    """
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    tracker.candidates = [rr_issue(43)]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]
    tracker.pr_comments[pr] = [
        IssueComment(
            id=f"IC_{n}", login=RR_SELF,
            body=f"<!-- switchboard:response-round n={n} bots={RR_BOT} self={RR_SELF} -->",
        )
        for n in (1, 2)
    ]

    restarted, _, _, _ = _rr_harness(tmp_path, monkeypatch)
    assert restarted._review_last_poll_at is None     # genuinely fresh process state
    assert await restarted._poll_review_responses(tracker) == []
    assert tracker.sole_status_swaps == []
    assert scheduler_mod.CAP_MARKER in tracker.comments[0][1]


async def test_review_poll_skips_a_parked_issue(tmp_path, monkeypatch):
    """AC 4: `_park` leaves the status label in place, so a hand-parked issue
    still reads `status:human-review`. Triggering it would burn a round on
    `_should_dispatch`'s park refusal and drop the issue from the state list."""
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    issue = rr_issue(43)
    issue.labels.append("status:parked")
    tracker.candidates = [issue]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]

    assert await orch._poll_review_responses(tracker) == []
    assert (tracker.comments, tracker.sole_status_swaps) == ([], [])


@pytest.mark.parametrize(
    "prs,unbindable",
    [
        # (i) the open PR does not close this issue: a real mis-binding.
        ([{"id": "PR_a", "number": 500, "head_sha": "a" * 40, "closes": [99]}], True),
        # (ii) two open PRs on one branch: ambiguous write target.
        ([{"id": "PR_a", "number": 500, "head_sha": "a" * 40, "closes": [43]},
          {"id": "PR_b", "number": 501, "head_sha": "b" * 40, "closes": [43]}], True),
        # (iii) ZERO open PRs — the NORMAL post-merge state, not an anomaly.
        ([], False),
    ],
    ids=["closes-mismatch", "two-open-prs", "no-open-pr"],
)
async def test_review_poll_binding_failures(
    tmp_path, monkeypatch, capfd, prs, unbindable
):
    """AC 3, the issue-first reachable set. The sub-poll CONSTRUCTS the head ref
    from the issue it holds, so "head ref unparsable" is unreachable here.

    Zero open PRs skips QUIETLY; the other two are flagged UNBINDABLE — loud,
    never a silent drop. No relabel and no crash in all three.
    """
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    issue = rr_issue(43)
    tracker.candidates = [issue]
    tracker.open_prs["switchboard/issue-43"] = prs
    tracker.pr_review_threads[500] = [rr_thread((RR_BOT, 0))]

    assert await orch._poll_review_responses(tracker) == []
    assert (tracker.comments, tracker.sole_status_swaps) == ([], [])
    assert "status:human-review" in issue.labels
    assert ("unbindable=True" in capfd.readouterr().err) is unbindable


async def test_review_poll_disabled_when_the_app_identity_is_unset(
    tmp_path, monkeypatch, capfd
):
    """AC 5: `bot_logins` configured but `SB_APP_BOT_LOGIN` unset => ZERO API
    calls and ONE log line.

    Reachable via the documented GITHUB_TOKEN dogfood path, under which worker
    replies carry the OPERATOR's login and no login-based identification can
    work. GATE FIRST: both checks precede the issue fetch, so copying the fold
    poll's fetch-then-gate shape verbatim would fail this.
    """
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch, login=None)
    tracker.candidates = [rr_issue(43)]
    _bind_pr(tracker, 43)

    assert await orch._poll_review_responses(tracker) == []
    assert tracker.api_calls == 0
    err = capfd.readouterr().err
    assert err.count("review-response disabled") == 1

    # Logged ONCE per process, not once per cycle: a per-poll repeat would bury
    # every other line in the runner log at the fold cadence.
    orch._review_last_poll_at = None
    await orch._poll_review_responses(tracker)
    assert "review-response disabled" not in capfd.readouterr().err
    assert tracker.api_calls == 0


async def test_review_poll_disabled_by_default_makes_no_calls(harness, monkeypatch):
    """The SHIPPED default: an empty `bot_logins` disables the loop at zero API
    cost, before any identity check — so it costs nothing even with the App set.
    """
    orch, tracker, _, _ = harness
    _enable_app_identity(monkeypatch)
    tracker.candidates = [rr_issue(43)]

    assert await orch._poll_review_responses(tracker) == []
    assert tracker.api_calls == 0


async def test_review_trigger_resets_a_spent_session_budget(tmp_path, monkeypatch):
    """AC 6: a PR whose issue has `spent == cap` still dispatches a responder.

    The per-role counter is cumulative across the issue's life and is otherwise
    cleared only by unpark — so the multi-session PRs that attract the MOST
    findings would arrive here spent and park on the very first response
    dispatch. (#43 itself sat parked at 3/3.) Every role is reset; the verify
    budget is unreachable from `todo`, so dropping it is harmless.
    """
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    issue = rr_issue(43)
    tracker.candidates = [issue]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]
    orch.sessions_per_issue[(issue.id, IMPLEMENT_ROLE)] = 2   # the configured cap
    orch.sessions_per_issue[(issue.id, VERIFY_ROLE)] = 2

    assert await orch._poll_review_responses(tracker) == ["43"]
    assert orch.sessions_per_issue == {}
    assert orch._tick_wakeup.is_set()      # dispatch is woken, not left to idle


async def test_review_poll_runs_inside_the_tick(tmp_path, monkeypatch, capfd):
    """Wiring: the sub-poll is reached from `_tick`, on the fold cadence, and a
    second tick inside the interval does not re-poll."""
    orch, tracker, _, _ = _rr_harness(tmp_path, monkeypatch)
    tracker.candidates = [rr_issue(43)]
    pr = _bind_pr(tracker, 43)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]

    def markers():
        return [b for _, b in tracker.comments if "response-round" in b]

    await orch._tick()
    assert "review-response triggered" in capfd.readouterr().err
    assert len(markers()) == 1

    await orch._tick()   # inside REVIEW_POLL_INTERVAL_MS: no second round
    assert len(markers()) == 1
