"""Orchestration state machine, poll loop, retries, reconciliation.

implements: core §7 (state machine), §8 (polling/scheduling/reconciliation),
            §16 (reference algorithms), §6.2 (reload)
overridden by: spec/SPEC.md §1 (worker turns are `claude -p` invocations resumed
            by session id), SPEC.md §4 owned extension: per-issue session cap
            with parking ("caps as diagnostic checkpoints" — when
            agent.max_sessions_per_issue worker sessions have been spent on one
            issue in this process lifetime, the orchestrator releases the claim,
            posts ONE notification comment on the issue, preserves the
            workspace/logs, and stops re-dispatching until the issue's
            updated_at changes. This is a deliberate, documented exception to
            the core §11.5 no-tracker-writes posture.)

Single-authority rule (core §7.4): all scheduling state lives in this class and
is mutated only from the event loop; workers report outcomes, they never mutate.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .log import log
from .prompt import render_prompt
from .runner import ClaudeRunner
from .tracker import GitHubTracker
from .types import (
    AgentEvent,
    Issue,
    RetryEntry,
    TrackerError,
    WorkflowError,
)
from .workflow import Config, load_workflow, validate_dispatch
from .workspace import WorkspaceManager

CONTINUATION_DELAY_MS = 1000       # core §8.4 fixed continuation delay
FAILURE_BASE_BACKOFF_MS = 10000    # core §8.4 failure backoff base

CONTINUATION_PROMPT = (
    "Continue working the same issue in this workspace. Do not restart from "
    "scratch: review your progress so far, then finish the remaining work, "
    "verify against the acceptance criteria, and hand off as instructed in the "
    "original task prompt."
)


@dataclass
class RunningEntry:
    """core §16.4 running-entry shape (claude-bound field names), trimmed to
    the fields orchestration actually consumes (the §13.3 snapshot surface and
    its token accounting were removed as unused — restore from git if a status
    endpoint lands)."""
    task: asyncio.Task
    identifier: str
    issue: Issue
    session_id: str | None = None
    last_event_at: datetime | None = None  # stall-detection anchor (§8.5)
    retry_attempt: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cancelled_by_reconciliation: bool = False


class WorkerFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Orchestrator:
    def __init__(self, workflow_path: Path):
        self.workflow_path = workflow_path.resolve()
        self._defn = None
        self._cfg: Config | None = None
        self._workflow_mtime: float | None = None

        # core §4.1.8 runtime state
        self.running: dict[str, RunningEntry] = {}
        self.claimed: set[str] = set()
        self.retry_attempts: dict[str, RetryEntry] = {}

        # owned extension state (SPEC.md §4 session cap / parking)
        self.sessions_per_issue: dict[str, int] = {}
        self.parked: dict[str, str] = {}  # issue_id -> updated_at iso at park time

        self._stopping = False
        self._workflow_broken: str | None = None  # §5.5 dispatch block reason
        self._tick_wakeup: asyncio.Event = asyncio.Event()
        # One shared HTTP client for the process lifetime (core §11.2 timeout).
        # Per-call GitHubTracker instances borrow it, so pools/handshakes are
        # reused and there is exactly one thing to close at shutdown.
        self._http: httpx.AsyncClient | None = None

    # -- component wiring (config-derived views over shared resources) ----------

    def _components(self) -> tuple[GitHubTracker, WorkspaceManager, ClaudeRunner]:
        cfg = self._cfg
        assert cfg is not None
        tracker = GitHubTracker(cfg.tracker(), client=self._http)
        wsm = WorkspaceManager(cfg.workspace_root(), cfg.hooks())
        runner = ClaudeRunner(cfg.claude())
        return tracker, wsm, runner

    # -- workflow load / reload (core §5.1, §6.2) -------------------------------

    def _load_workflow(self, *, initial: bool) -> None:
        try:
            mtime = self.workflow_path.stat().st_mtime
            defn = load_workflow(self.workflow_path)
            cfg = Config(defn, self.workflow_path.parent)
            validate_dispatch(cfg)
        except (WorkflowError, OSError) as exc:
            if initial:
                raise
            # §6.2: invalid reload keeps last known good config for
            # reconciliation, but §5.5 blocks NEW dispatches until fixed.
            self._workflow_broken = str(exc)
            log("workflow reload invalid; keeping last good config, "
                "dispatch blocked until fixed", error=str(exc))
            return
        self._workflow_broken = None
        if self._workflow_mtime is not None and mtime == self._workflow_mtime:
            return
        self._defn, self._cfg, self._workflow_mtime = defn, cfg, mtime
        if not initial:
            log("workflow reloaded", path=str(self.workflow_path))

    def _maybe_reload(self) -> None:
        try:
            if self.workflow_path.stat().st_mtime != self._workflow_mtime:
                self._load_workflow(initial=False)
        except OSError as exc:
            self._workflow_broken = str(exc)
            log("workflow stat failed; keeping last good config, "
                "dispatch blocked until fixed", error=str(exc))

    # -- service lifecycle (core §16.1) -----------------------------------------

    async def run(self) -> None:
        self._load_workflow(initial=True)  # startup validation failure aborts (§6.3)
        cfg = self._cfg
        assert cfg is not None
        log("orchestrator starting", workflow=str(self.workflow_path),
            repo=cfg.tracker().repo, workspace_root=str(cfg.workspace_root()))

        self._http = httpx.AsyncClient(timeout=30.0)  # core §11.2 network timeout
        try:
            await self._startup_terminal_cleanup()

            while not self._stopping:
                try:
                    await self._tick()
                except Exception as exc:  # a tick must never kill the service (§14.2)
                    log("tick error", error=repr(exc))
                interval = (self._cfg.polling_interval_ms() if self._cfg else 30000) / 1000
                self._tick_wakeup.clear()
                try:
                    await asyncio.wait_for(self._tick_wakeup.wait(), timeout=interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            # shutdown() has already cancelled and gathered workers by the
            # time the loop observes _stopping, so no request is in flight.
            await self._http.aclose()
            self._http = None

    async def shutdown(self) -> None:
        self._stopping = True
        for entry in list(self.running.values()):
            entry.cancelled_by_reconciliation = True
            entry.task.cancel()
        for retry in list(self.retry_attempts.values()):
            retry.timer_handle.cancel()
        self.retry_attempts.clear()
        if self.running:
            await asyncio.gather(*(e.task for e in self.running.values()),
                                 return_exceptions=True)
        self._tick_wakeup.set()

    async def _startup_terminal_cleanup(self) -> None:
        """core §8.6: remove workspaces for issues already terminal."""
        cfg = self._cfg
        assert cfg is not None
        tracker, wsm, _ = self._components()
        try:
            terminal = await tracker.fetch_issues_by_states(cfg.tracker().terminal_states)
        except TrackerError as exc:
            log("startup terminal cleanup fetch failed; continuing", error=str(exc))
            return
        await wsm.cleanup_terminal([i.identifier for i in terminal])

    # -- poll tick (core §16.2) --------------------------------------------------

    async def _tick(self) -> None:
        self._maybe_reload()
        await self._reconcile_running()

        if self._workflow_broken is not None:
            # §5.5: workflow file read/YAML errors block new dispatches until
            # fixed (reconciliation above stays active on last-good config).
            log("workflow broken; skipping dispatch this tick",
                error=self._workflow_broken)
            return

        cfg = self._cfg
        assert cfg is not None
        try:
            validate_dispatch(cfg)
        except WorkflowError as exc:
            log("dispatch preflight failed; skipping dispatch this tick", error=str(exc))
            return

        tracker, _, _ = self._components()
        try:
            issues = await tracker.fetch_candidate_issues()
        except TrackerError as exc:
            log("candidate fetch failed; skipping dispatch this tick", error=str(exc))
            return

        for issue in self._sort_for_dispatch(issues):
            if self._stopping:  # shutdown arrived while we awaited the fetch
                return
            if self._available_slots() <= 0:
                break
            if self._should_dispatch(issue):
                await self._dispatch(issue, attempt=None)

    @staticmethod
    def _sort_for_dispatch(issues: list[Issue]) -> list[Issue]:
        """core §8.2 sort: priority asc (null last), created_at oldest, identifier."""
        def key(i: Issue):
            return (
                i.priority if i.priority is not None else 1 << 30,
                i.created_at.timestamp() if i.created_at else float("inf"),
                i.identifier,
            )
        return sorted(issues, key=key)

    def _available_slots(self) -> int:
        cfg = self._cfg
        assert cfg is not None
        return max(cfg.agent().max_concurrent_agents - len(self.running), 0)

    def _state_slots_available(self, state: str) -> bool:
        cfg = self._cfg
        assert cfg is not None
        by_state = cfg.agent().max_concurrent_agents_by_state
        limit = by_state.get(state.lower())
        if limit is None:
            return True
        count = sum(1 for e in self.running.values()
                    if e.issue.state.lower() == state.lower())
        return count < limit

    def _should_dispatch(self, issue: Issue) -> bool:
        """core §8.2 candidate selection + owned parking gate."""
        cfg = self._cfg
        assert cfg is not None
        t = cfg.tracker()
        if not (issue.id and issue.identifier and issue.title and issue.state):
            return False
        state = issue.state.lower()
        if state not in t.active_states or state in t.terminal_states:
            return False
        if t.required_labels and not all(
                lbl and lbl in issue.labels for lbl in t.required_labels):
            return False
        if issue.id in self.running or issue.id in self.claimed:
            return False
        # owned parking gate: an updated_at change (human touched it) unparks
        if issue.id in self.parked:
            marker = issue.updated_at.isoformat() if issue.updated_at else ""
            if self.parked[issue.id] == marker:
                return False
            del self.parked[issue.id]
            self.sessions_per_issue.pop(issue.id, None)
            log("issue unparked (tracker activity observed)",
                issue_id=issue.id, issue_identifier=issue.identifier)
        if not self._state_slots_available(issue.state):
            return False
        if state == "todo":
            for b in issue.blocked_by:
                if (b.state or "").lower() not in t.terminal_states \
                        and (b.state or "").lower() != "closed":
                    return False
        return True

    # -- dispatch / worker (core §16.4, §16.5) ------------------------------------

    async def _dispatch(self, issue: Issue, attempt: int | None) -> None:
        cfg = self._cfg
        assert cfg is not None
        # The cap is always positive (workflow.py coerces invalid values back
        # to the default) — parking cannot be configured off.
        cap = cfg.agent().max_sessions_per_issue
        spent = self.sessions_per_issue.get(issue.id, 0)
        if spent >= cap:
            await self._park(issue, f"session cap reached ({spent}/{cap})")
            return

        task = asyncio.create_task(self._worker(issue, attempt))
        entry = RunningEntry(task=task, identifier=issue.identifier, issue=issue,
                             retry_attempt=attempt)
        self.running[issue.id] = entry
        self.claimed.add(issue.id)
        self._cancel_retry(issue.id)
        self.sessions_per_issue[issue.id] = spent + 1
        log("dispatched", issue_id=issue.id, issue_identifier=issue.identifier,
            attempt=attempt, session_number=spent + 1)
        task.add_done_callback(
            lambda t, iid=issue.id: self._on_worker_done(iid, t))

    async def _worker(self, issue: Issue, attempt: int | None) -> None:
        """One worker session: workspace -> before_run -> turn loop (core §16.5)."""
        cfg = self._cfg
        assert cfg is not None
        defn = self._defn
        assert defn is not None
        tracker, wsm, runner = self._components()
        claude_cfg = cfg.claude()

        ws = await wsm.create_for_issue(issue.identifier)          # WorkspaceError -> abnormal
        try:
            await wsm.run_before_run(ws)                           # HookError -> abnormal
        except Exception as exc:
            raise WorkerFailure(f"before_run hook error: {exc}") from exc

        session_id: str | None = None
        cumulative_cost = 0.0
        turn_number = 1
        dispatch_state = issue.state.lower()
        try:
            while True:
                if turn_number == 1:
                    prompt = render_prompt(defn.prompt_template, issue, attempt)
                else:
                    prompt = CONTINUATION_PROMPT  # §7.1: don't resend the task prompt
                result = await runner.run_turn(
                    ws.path, prompt, resume_session_id=session_id,
                    on_event=self._on_agent_event, issue_id=issue.id)
                cumulative_cost += result.cost_usd
                entry = self.running.get(issue.id)
                if result.status != "succeeded":
                    raise WorkerFailure(result.error or result.status)
                session_id = result.session_id or session_id

                try:  # §16.5: re-check tracker state between turns
                    refreshed = await tracker.fetch_issue_states_by_ids([issue.id])
                except TrackerError as exc:
                    raise WorkerFailure(f"issue state refresh error: {exc}") from exc
                if refreshed:
                    issue = refreshed[0]
                    if entry:
                        entry.issue = issue
                # SPEC.md §4 override of core §16.5 (role-pinned sessions):
                # the turn-1 prompt was rendered from dispatch-time state, so
                # ANY state change — active -> active included (triage PASS) —
                # ends the session; re-dispatch picks it up in the new role.
                if issue.state.lower() != dispatch_state:
                    break
                # core §11.1(3): required labels gate continuation here too —
                # reconciliation only sees label removal at the next poll
                # tick, which is too late to stop the next turn from firing.
                t = cfg.tracker()
                if t.required_labels and not all(
                        lbl in issue.labels for lbl in t.required_labels):
                    log("required label removed; ending session normally",
                        issue_id=issue.id, issue_identifier=issue.identifier)
                    break
                if claude_cfg.max_budget_usd is not None \
                        and cumulative_cost >= claude_cfg.max_budget_usd:
                    log("worker budget ceiling reached; ending session normally",
                        issue_id=issue.id, issue_identifier=issue.identifier,
                        cost_usd=round(cumulative_cost, 4))
                    break
                if turn_number >= cfg.agent().max_turns:
                    break
                turn_number += 1
        finally:
            await wsm.run_after_run(ws)                            # ignored on failure

    def _on_worker_done(self, issue_id: str, task: asyncio.Task) -> None:
        """core §16.6 worker exit handling."""
        entry = self.running.pop(issue_id, None)
        if entry is None:
            return

        if entry.cancelled_by_reconciliation or task.cancelled():
            self.claimed.discard(issue_id)
            log("worker cancelled", issue_id=issue_id,
                issue_identifier=entry.identifier)
            return

        exc = task.exception()
        if exc is None:
            self._schedule_retry(issue_id, entry.identifier, attempt=1,
                                 delay_ms=CONTINUATION_DELAY_MS)
            log("worker completed", issue_id=issue_id,
                issue_identifier=entry.identifier,
                session_id=entry.session_id, outcome="completed")
        else:
            attempt = (entry.retry_attempt or 0) + 1
            self._schedule_retry(issue_id, entry.identifier, attempt=attempt,
                                 delay_ms=self._failure_backoff_ms(attempt))
            log("worker failed", issue_id=issue_id,
                issue_identifier=entry.identifier,
                session_id=entry.session_id, outcome="failed", error=str(exc))

    def _failure_backoff_ms(self, attempt: int) -> int:
        cfg = self._cfg
        assert cfg is not None
        return min(FAILURE_BASE_BACKOFF_MS * 2 ** (attempt - 1),
                   cfg.agent().max_retry_backoff_ms)

    # -- retries (core §8.4, §16.6) -------------------------------------------------

    def _cancel_retry(self, issue_id: str) -> None:
        entry = self.retry_attempts.pop(issue_id, None)
        if entry:
            entry.timer_handle.cancel()

    def _schedule_retry(self, issue_id: str, identifier: str, attempt: int,
                        delay_ms: int) -> None:
        self._cancel_retry(issue_id)
        self.claimed.add(issue_id)
        handle = asyncio.get_event_loop().call_later(
            delay_ms / 1000,
            lambda: asyncio.ensure_future(self._on_retry_timer(issue_id)))
        self.retry_attempts[issue_id] = RetryEntry(
            issue_id=issue_id, identifier=identifier, attempt=attempt,
            timer_handle=handle)

    async def _on_retry_timer(self, issue_id: str) -> None:
        entry = self.retry_attempts.pop(issue_id, None)
        if entry is None or self._stopping:
            return
        tracker, _, _ = self._components()
        try:
            candidates = await tracker.fetch_candidate_issues()
        except Exception as exc:
            # ANY failure here (TrackerError or a payload-shape bug) must
            # reschedule rather than propagate: the retry entry is already
            # popped, so an escaped exception would strand the claim forever.
            log("retry poll failed; rescheduling", issue_id=issue_id,
                issue_identifier=entry.identifier, error=repr(exc))
            self._schedule_retry(issue_id, entry.identifier, entry.attempt + 1,
                                 self._failure_backoff_ms(entry.attempt + 1))
            return
        issue = next((i for i in candidates if i.id == issue_id), None)
        if issue is None:
            self.claimed.discard(issue_id)
            log("claim released (issue no longer a candidate)", issue_id=issue_id,
                issue_identifier=entry.identifier)
            return
        if self._available_slots() <= 0:
            self._schedule_retry(issue_id, entry.identifier, entry.attempt + 1,
                                 self._failure_backoff_ms(entry.attempt + 1))
            return
        # re-run eligibility checks (blockers/labels/state may have changed)
        self.claimed.discard(issue_id)
        if self._should_dispatch(issue):
            await self._dispatch(issue, attempt=entry.attempt)
        else:
            log("claim released (issue no longer eligible)", issue_id=issue_id,
                issue_identifier=entry.identifier)

    # -- reconciliation (core §8.5, §16.3) --------------------------------------------

    async def _reconcile_running(self) -> None:
        cfg = self._cfg
        assert cfg is not None
        # Part A: stall detection
        stall_ms = cfg.claude().stall_timeout_ms
        if stall_ms > 0:
            now = datetime.now(timezone.utc)
            for issue_id, entry in list(self.running.items()):
                anchor = entry.last_event_at or entry.started_at
                if (now - anchor).total_seconds() * 1000 > stall_ms:
                    log("stalled session; terminating and retrying",
                        issue_id=issue_id, issue_identifier=entry.identifier,
                        session_id=entry.session_id)
                    await self._terminate(issue_id, cleanup=False, retry=True)

        if not self.running:
            return
        # Part B: tracker state refresh
        tracker, _, _ = self._components()
        try:
            refreshed = await tracker.fetch_issue_states_by_ids(list(self.running))
        except TrackerError as exc:
            log("state refresh failed; keeping workers running", error=str(exc))
            return
        t = cfg.tracker()
        for issue in refreshed:
            entry = self.running.get(issue.id)
            if entry is None:
                continue
            state = issue.state.lower()
            if state in t.terminal_states:
                await self._terminate(issue.id, cleanup=True, retry=False)
            elif state in t.active_states:
                # core §11.1(3)/§8.2: required labels gate continuation too —
                # pulling a required label mid-run stops the worker.
                if t.required_labels and not all(
                        lbl in issue.labels for lbl in t.required_labels):
                    log("required label removed; releasing worker",
                        issue_id=issue.id, issue_identifier=entry.identifier)
                    await self._terminate(issue.id, cleanup=False, retry=False)
                else:
                    entry.issue = issue
            else:
                await self._terminate(issue.id, cleanup=False, retry=False)

    async def _terminate(self, issue_id: str, *, cleanup: bool, retry: bool) -> None:
        """Take authority over a running worker (§8.5): pop the entry *before*
        cancelling so the done-callback becomes a no-op, then decide retry vs
        release ourselves — deterministic regardless of callback ordering."""
        entry = self.running.pop(issue_id, None)
        if entry is None:
            return
        entry.cancelled_by_reconciliation = True
        entry.task.cancel()
        await asyncio.gather(entry.task, return_exceptions=True)
        if retry:
            attempt = (entry.retry_attempt or 0) + 1
            self._schedule_retry(issue_id, entry.identifier, attempt=attempt,
                                 delay_ms=self._failure_backoff_ms(attempt))
        else:
            self.claimed.discard(issue_id)
        if cleanup:
            _, wsm, _ = self._components()
            await wsm.cleanup_terminal([entry.identifier])

    # -- parking (owned extension, SPEC.md §4) --------------------------------------

    async def _park(self, issue: Issue, reason: str) -> None:
        self._cancel_retry(issue.id)
        self.parked[issue.id] = issue.updated_at.isoformat() if issue.updated_at else ""
        log("ISSUE PARKED — human attention needed", issue_id=issue.id,
            issue_identifier=issue.identifier, reason=reason,
            workspace_preserved=True)
        tracker, wsm, _ = self._components()
        body = (
            f"**Switchboard parked this issue** — {reason}.\n\n"
            f"The orchestrator will not dispatch it again until the issue is "
            f"updated (edit, label change, or comment). The per-issue workspace "
            f"is preserved for diagnosis at `{wsm.path_for(issue.identifier)}`."
        )
        # Hold the claim while the marker settles: our own comment bumps
        # updatedAt, and a poll tick landing between the comment and the
        # marker re-fetch below would otherwise see marker != updatedAt,
        # unpark, and dispatch a bonus session past the cap.
        self.claimed.add(issue.id)
        try:
            await tracker.add_issue_comment(issue.id, body)
            # Re-fetch so the park marker is the POST-comment value —
            # otherwise the next poll would see the bump, unpark, and loop
            # the cap forever (the exact failure this extension prevents).
            refreshed = await tracker.fetch_issue_states_by_ids([issue.id])
            if refreshed and refreshed[0].updated_at:
                self.parked[issue.id] = refreshed[0].updated_at.isoformat()
        except TrackerError as exc:
            log("parking comment/marker refresh failed (issue stays parked "
                "on pre-comment marker)", issue_id=issue.id, error=str(exc))
        finally:
            self.claimed.discard(issue.id)

    # -- agent events (core §10.4 consumer) --------------------------------------------

    def _on_agent_event(self, issue_id: str, event: AgentEvent) -> None:
        entry = self.running.get(issue_id)
        if entry is None:
            return
        entry.last_event_at = event.timestamp
        if event.event == "session_started":
            sid = event.payload.get("session_id")
            if sid:
                entry.session_id = sid
                log("session started", issue_id=issue_id,
                    issue_identifier=entry.identifier, session_id=sid)
