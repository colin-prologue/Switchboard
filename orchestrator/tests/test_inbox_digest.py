"""Operator inbox digest — "what awaits YOU" by body edit (issue #192).

Three claims carry this suite, and each one is a failure the feature could
plausibly ship with:

- **A digest run must be invisible to every tracked issue.** The 2026-07-02
  incident (OBS-022) was a notification comment bumping the issue's
  `updatedAt`, which was the unpark signal — an unbounded spend loop that 110
  passing tests never saw, because the fake did not model GitHub's
  comment→`updatedAt` echo. The shared `FakeTracker` DOES model that echo
  (`add_issue_comment` bumps `updated_at`, and so does every label write), so
  the guard here is not "assert no comments were recorded" but the stronger
  "assert no tracked issue's `updated_at` moved" — which fails on any write
  path, including ones nobody thought to enumerate.

- **Unchanged content must not write.** The snapshot model is the whole reason
  a report can ride the poll loop, and it dies quietly: put one timestamp in
  the compared region and the digest writes every cycle, becoming the noise
  surface `board_sanity` refuses to be. The compare must therefore be tested
  ACROSS a render round trip, not against a hand-built string.

- **A failed read must not render as an empty section.** "No PR is open" and
  "we could not ask" are opposite answers and the operator acts differently on
  each, so the unreadable path is asserted on the rendered text, not on a flag.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.cap_report import CAP_REPORT_HEADING
from orchestrator.inbox_digest import (
    CONTENT_SENTINEL,
    FAIL_REVIEW_HEADING,
    MAX_COMMENT_SCANS,
    PARK_COMMENT_PREFIX,
    Inbox,
    IssueRow,
    collect_inbox,
    content_of,
    marker_line,
    park_reason,
    parse_watermark,
    render_body,
    render_content,
    run_digest,
    waiting_states,
)
from orchestrator.types import Issue, IssueComment, TrackerConfig, TrackerError
from orchestrator.workflow import Config, WorkflowError, load_workflow

from test_integration import (  # the shared fakes; this suite adds no second set
    WORKFLOW_TMPL,
    _build_harness,
    make_issue,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


# --- helpers ------------------------------------------------------------------


def issue(
    number: int,
    *,
    state: str = "human review",
    labels: tuple[str, ...] | None = None,
    updated: datetime | None = None,
) -> Issue:
    return Issue(
        id=f"node-{number}",
        identifier=str(number),
        title=f"Issue {number}",
        description=None,
        priority=None,
        state=state,
        branch_name=None,
        url=f"https://github.com/acme/api/issues/{number}",
        labels=list(labels if labels is not None else (f"status:{state.replace(' ', '-')}",)),
        updated_at=updated,
    )


def comment(body: str, *, created: datetime | None = None, ident: str = "c1") -> IssueComment:
    return IssueComment(id=ident, body=body, login="switchboard", created_at=created)


def tracker_cfg(**kw) -> TrackerConfig:
    base = dict(
        kind="github",
        repo="acme/api",
        endpoint="https://api.github.com/graphql",
        api_key="k",
        required_labels=[],
        active_states=["todo", "in progress"],
        terminal_states=["done", "closed"],
        gate_states=["drafting", "decision", "plan review"],
    )
    base.update(kw)
    return TrackerConfig(**base)


class DigestTracker:
    """A tracker that serves ONE digest issue and refuses every issue write.

    The refusals are `AssertionError`, not recorded calls: `collect_inbox` and
    `run_digest` swallow exceptions by design (a report must never halt the
    poll), so a write surface that merely recorded itself would be swallowed
    into a passing test. `AssertionError` is not caught by the `except
    Exception` in either — it is, but the *outcome status* then changes, and
    the assertions below read that status. Belt and braces: the invisibility
    invariant is also asserted end-to-end against the shared fake, which models
    the `updatedAt` echo the real API has.
    """

    def __init__(self, body: str = "", *, digest_id: str = "D1") -> None:
        self.digest_id = digest_id
        self.body = body
        self.state = "open"
        self.comments_by_number: dict[str, list[IssueComment]] = {}
        self.prs: list[dict] = []
        self.prs_error: Exception | None = None
        self.comment_error: Exception | None = None
        self.body_writes: list[str] = []
        self.update_error: Exception | None = None
        self.comment_fetches: list[str] = []
        self.state_reads = 0
        # Fires immediately BEFORE the write's guarding re-read — the only way
        # to model a concurrent operator edit at the point the guard exists for.
        self.on_pre_write_read = None

    async def fetch_issue_states_by_ids(self, ids):
        self.state_reads += 1
        if self.on_pre_write_read is not None and self.state_reads == 2:
            self.on_pre_write_read()
        if self.digest_id not in ids:
            return []
        return [
            Issue(
                id=self.digest_id, identifier="999", title="Switchboard operator inbox",
                description=self.body, priority=None, state=self.state,
                branch_name=None, url=None, labels=[],
            )
        ]

    async def update_issue_body(self, issue_id, body):
        if self.update_error is not None:
            raise self.update_error
        assert issue_id == self.digest_id, "the digest wrote to a foreign issue"
        self.body_writes.append(body)
        self.body = body

    async def fetch_open_prs_repo_wide(self):
        if self.prs_error is not None:
            raise self.prs_error
        return [dict(p) for p in self.prs]

    async def fetch_issue_comments(self, issue_number):
        self.comment_fetches.append(str(issue_number))
        if self.comment_error is not None:
            raise self.comment_error
        return list(self.comments_by_number.get(str(issue_number), []))

    # Every write surface the feature must never touch on a tracked issue.
    async def add_issue_comment(self, issue_id, body):
        raise AssertionError(f"the digest commented on {issue_id}")

    async def add_labels(self, issue_id, label_names):
        raise AssertionError(f"the digest wrote labels {label_names} on {issue_id}")

    async def remove_labels(self, issue_id, label_names):
        raise AssertionError(f"the digest removed labels {label_names} on {issue_id}")

    async def set_sole_status_label(self, issue_id, label, **kw):
        raise AssertionError(f"the digest swapped a status label on {issue_id}")


# --- what the digest enumerates ------------------------------------------------


def test_waiting_states_are_the_gates_plus_the_handoff_target():
    """Config-derived, never a literal list — a gate is a per-project property."""
    states = waiting_states(tracker_cfg())
    assert states == {"drafting", "decision", "plan review", "human review"}


def test_waiting_states_follow_a_stance_that_moves_the_handoff():
    """At `prototype` the handoff target is an agent QA state, and the digest
    must follow the config rather than a pinned `human review`."""
    states = waiting_states(tracker_cfg(
        handoff_label="status:review",
        active_states=["todo", "in progress", "review"],
        gate_states=["blocked"],
    ))
    assert states == {"blocked", "review"}


async def test_gate_issues_open_prs_parks_and_window_artifacts_are_enumerated():
    tracker = DigestTracker()
    tracker.prs = [
        {"number": 42, "title": "feat: thing", "url": "u42",
         "is_draft": False, "head_ref": "switchboard/issue-7"},
    ]
    gate = issue(7, state="human review", updated=T0 + timedelta(minutes=5))
    parked = issue(
        8, state="todo",
        labels=("status:todo", "status:parked"),
        updated=T0 - timedelta(days=9),
    )
    tracker.comments_by_number["8"] = [
        comment(f"{PARK_COMMENT_PREFIX}implement budget exhausted (2/2 sessions).",
                created=T0 - timedelta(days=9)),
    ]
    tracker.comments_by_number["7"] = [
        comment(f"{FAIL_REVIEW_HEADING}\n\nthe verdict",
                created=T0 + timedelta(minutes=4)),
        comment(f"{CAP_REPORT_HEADING}\n\nthe report",
                created=T0 + timedelta(minutes=5)),
    ]

    inbox = await collect_inbox(
        tracker, tracker_cfg(), [gate, parked], watermark=T0)

    assert [r.number for r in inbox.awaiting] == ["7"]
    assert [r.number for r in inbox.parked] == ["8"]
    assert [r.number for r in inbox.pulls] == [42]
    assert [r.issue_number for r in inbox.verdicts] == ["7"]
    assert [r.issue_number for r in inbox.cap_reports] == ["7"]
    # The park REASON, not just the label — the whole point of scanning parked
    # issues on every cycle.
    assert inbox.parked[0].note == "implement budget exhausted (2/2 sessions)"


async def test_a_parked_gate_issue_is_listed_once_as_parked():
    """`_park` leaves the status label in place, so a parked issue still reads
    as its gate state. Listing it in both sections would tell the operator to
    act on a ticket the orchestrator has already stopped touching."""
    tracker = DigestTracker()
    parked = issue(9, state="human review",
                   labels=("status:human-review", "status:parked"))
    inbox = await collect_inbox(tracker, tracker_cfg(), [parked], watermark=T0)
    assert [r.number for r in inbox.awaiting] == []
    assert [r.number for r in inbox.parked] == ["9"]


async def test_artifacts_before_the_watermark_are_outside_the_window():
    tracker = DigestTracker()
    touched = issue(7, state="human review", updated=T0 + timedelta(hours=1))
    tracker.comments_by_number["7"] = [
        comment(f"{FAIL_REVIEW_HEADING}\n\nold", created=T0 - timedelta(hours=1)),
        comment(f"{FAIL_REVIEW_HEADING}\n\nnew", created=T0 + timedelta(hours=1)),
    ]
    inbox = await collect_inbox(tracker, tracker_cfg(), [touched], watermark=T0)
    assert [r.created_at for r in inbox.verdicts] == [T0 + timedelta(hours=1)]


async def test_the_digest_issue_itself_is_never_enumerated():
    """It carries no `status:*` label so it cannot reach the gate section, but
    it IS in the tick's open-issue set and would otherwise be scanned for its
    own comments every cycle."""
    tracker = DigestTracker()
    own = Issue(id="D1", identifier="999", title="Switchboard operator inbox",
                description=None, priority=None, state="", branch_name=None,
                url=None, labels=[], updated_at=T0 + timedelta(hours=1))
    await collect_inbox(tracker, tracker_cfg(), [own],
                        watermark=T0, digest_issue_id="D1")
    assert tracker.comment_fetches == []


async def test_the_comment_scan_is_bounded_and_says_so():
    """A bounded digest that reads as complete is worse than one that reports
    what it skipped."""
    tracker = DigestTracker()
    issues = [
        issue(n, state="human review", updated=T0 + timedelta(minutes=n))
        for n in range(1, MAX_COMMENT_SCANS + 6)
    ]
    inbox = await collect_inbox(tracker, tracker_cfg(), issues, watermark=T0)
    assert len(tracker.comment_fetches) == MAX_COMMENT_SCANS
    assert any("were not scanned" in n for n in inbox.notes)
    assert "5 issue(s)" in " ".join(inbox.notes)


# --- reads degrade, they never halt --------------------------------------------


async def test_a_failed_pr_read_renders_unreadable_not_empty():
    tracker = DigestTracker()
    tracker.prs_error = TrackerError("github_api_status", "502")
    inbox = await collect_inbox(tracker, tracker_cfg(), [], watermark=T0)
    body = render_content(inbox)
    assert "pulls" in inbox.unreadable
    assert "Could not be read this cycle" in body
    assert "No pull request is open" not in body


async def test_a_failed_comment_read_is_per_issue_and_reported():
    tracker = DigestTracker()
    tracker.comment_error = TrackerError("github_api_status", "502")
    gate = issue(7, state="human review", updated=T0 + timedelta(hours=1))
    inbox = await collect_inbox(tracker, tracker_cfg(), [gate], watermark=T0)
    # The label-derived section still rendered: one read failing must not cost
    # the operator the sections that succeeded.
    assert [r.number for r in inbox.awaiting] == ["7"]
    assert "artifacts" in inbox.unreadable
    assert any("could not be read" in n for n in inbox.notes)


async def test_collect_inbox_never_raises_even_when_every_read_fails():
    class Hostile:
        async def fetch_open_prs_repo_wide(self):
            raise RuntimeError("boom")

        async def fetch_issue_comments(self, issue_number):
            raise RuntimeError("boom")

    inbox = await collect_inbox(
        Hostile(), tracker_cfg(), [issue(7, updated=T0 + timedelta(hours=1))],
        watermark=T0)
    assert inbox.unreadable  # degraded, and it says so


# --- the body format ------------------------------------------------------------


def test_an_empty_inbox_says_so_explicitly_with_a_timestamp():
    """Distinguishable from a digest that failed to run — which is why the
    emptiness lives in the CONTENT and the timestamp lives in the header."""
    body = render_body(render_content(Inbox()), window_start=T0,
                       watermark=T0, changed_at=T0)
    assert "**Nothing awaits you.**" in body
    assert T0.isoformat() in body


def test_the_body_disclaims_liveness():
    """A stale timestamp here means "nothing changed", never "nothing ran" —
    fleet liveness is the health ticket's beat (a declared non-goal), so the
    body must not be readable as a liveness signal."""
    body = render_body(render_content(Inbox()), window_start=T0,
                       watermark=T0, changed_at=T0)
    assert "not that nothing is running" in body


def test_the_watermark_round_trips_through_the_body():
    """Durable in the body itself: a restart neither re-reports old artifacts
    nor silently drops the window."""
    later = T0 + timedelta(days=1)
    body = render_body("x", window_start=T0, watermark=later, changed_at=later)
    assert parse_watermark(body) == later


def test_a_body_the_operator_rewrote_past_the_marker_parses_as_none():
    assert parse_watermark("the operator's own notes") is None
    assert parse_watermark(None) is None
    assert parse_watermark(marker_line(T0).replace(T0.isoformat(), "not-a-date")) is None


def test_the_compared_region_carries_no_timestamp():
    """The load-bearing property of the whole design. A timestamp inside the
    compared content differs on every render, forces a write every cycle, and
    turns a snapshot into the noise surface `board_sanity` refuses to be."""
    inbox = Inbox(awaiting=(IssueRow(number="7", title="t", state="human review"),))
    first = render_body(render_content(inbox), window_start=T0,
                        watermark=T0, changed_at=T0)
    hours_later = T0 + timedelta(hours=6)
    second = render_body(render_content(inbox), window_start=hours_later,
                         watermark=hours_later, changed_at=hours_later)
    assert first != second                      # the header moved
    assert content_of(first) == content_of(second)  # the content did not


def test_content_of_a_body_with_no_sentinel_is_none():
    """A body the operator replaced wholesale compares unequal to any content,
    so the next cycle re-renders rather than treating it as up to date."""
    assert content_of("hand-written notes") is None


def test_park_reason_parses_the_comment_the_scheduler_actually_writes():
    """Binds the digest's parser to `_park`'s writer through ONE literal.

    Not a re-spelling of the prefix: `scheduler._park` imports
    `PARK_COMMENT_PREFIX` from the digest module, so a change to either side
    that breaks the pairing breaks this test.
    """
    from orchestrator import scheduler as scheduler_mod

    body = (
        f"{PARK_COMMENT_PREFIX}implement budget exhausted (2/2 sessions).\n\n"
        f"The orchestrator will not dispatch it again while it carries the "
        f"`{scheduler_mod.PARK_LABEL}` label."
    )
    assert park_reason(body) == "implement budget exhausted (2/2 sessions)"


def test_park_reason_declines_an_ordinary_comment():
    assert park_reason("looks good to me") is None
    assert park_reason("") is None


# --- read-compare-write ----------------------------------------------------------


async def test_unchanged_content_produces_no_write():
    tracker = DigestTracker()
    first = await run_digest(tracker, tracker_cfg(), [],
                             digest_issue_id="D1", now=T0)
    assert first.status == "written"
    assert len(tracker.body_writes) == 1

    later = await run_digest(tracker, tracker_cfg(), [],
                             digest_issue_id="D1", now=T0 + timedelta(days=1))
    assert later.status == "unchanged"
    assert len(tracker.body_writes) == 1  # still one


async def test_the_unchanged_path_does_not_advance_the_watermark():
    """Advancing it would close a window over artifacts nobody was shown."""
    tracker = DigestTracker()
    await run_digest(tracker, tracker_cfg(), [], digest_issue_id="D1", now=T0)
    outcome = await run_digest(tracker, tracker_cfg(), [],
                               digest_issue_id="D1", now=T0 + timedelta(days=1))
    assert outcome.watermark is None
    assert parse_watermark(tracker.body) == T0


async def test_a_body_changed_under_the_read_is_refused_not_clobbered():
    tracker = DigestTracker()

    def operator_edits():
        tracker.body = "I am editing this right now"

    tracker.on_pre_write_read = operator_edits
    outcome = await run_digest(tracker, tracker_cfg(), [],
                               digest_issue_id="D1", now=T0)
    assert outcome.status == "refused_clobber"
    assert tracker.body_writes == []
    assert tracker.body == "I am editing this right now"
    assert outcome.watermark is None  # retried next cycle


async def test_a_write_failure_is_reported_and_holds_the_window_open():
    tracker = DigestTracker()
    tracker.update_error = TrackerError("github_api_status", "502")
    outcome = await run_digest(tracker, tracker_cfg(), [],
                               digest_issue_id="D1", now=T0)
    assert outcome.status == "write_failed"
    assert outcome.watermark is None


async def test_a_verify_divergence_holds_the_window_open():
    """Verify-after-write is the fold-apply precedent: it cannot catch a
    clobber (it compares STORED against INTENDED) but it does catch a write
    that did not land as issued — and then the watermark must not move."""
    tracker = DigestTracker()

    async def mangling_write(issue_id, body):
        tracker.body_writes.append(body)
        tracker.body = body + "\nMANGLED BY THE SERVER"

    tracker.update_issue_body = mangling_write
    outcome = await run_digest(tracker, tracker_cfg(), [],
                               digest_issue_id="D1", now=T0)
    assert outcome.status == "verify_diverged"
    assert outcome.wrote is True
    assert outcome.watermark is None


async def test_an_unreadable_digest_issue_is_not_fatal():
    class Unreadable(DigestTracker):
        async def fetch_issue_states_by_ids(self, ids):
            raise TrackerError("github_api_status", "502")

    outcome = await run_digest(Unreadable(), tracker_cfg(), [],
                               digest_issue_id="D1", now=T0)
    assert outcome.status == "read_failed"


async def test_the_window_resumes_from_the_body_across_a_restart():
    """No in-memory state involved: a second `run_digest` with no fallback and
    no shared object reads its window start out of the body the first wrote."""
    tracker = DigestTracker()
    await run_digest(tracker, tracker_cfg(), [], digest_issue_id="D1", now=T0)

    verdict_at = T0 + timedelta(hours=2)
    gate = issue(7, state="human review", updated=verdict_at)
    tracker.comments_by_number["7"] = [
        comment(f"{FAIL_REVIEW_HEADING}\n\nposted after the first digest",
                created=verdict_at),
        # Predates the first digest's watermark: already outside the window,
        # and a restart must not resurrect it.
        comment(f"{CAP_REPORT_HEADING}\n\nold news",
                created=T0 - timedelta(hours=2)),
    ]
    outcome = await run_digest(tracker, tracker_cfg(), [gate],
                               digest_issue_id="D1", now=T0 + timedelta(hours=3))
    assert outcome.status == "written"
    assert [r.created_at for r in outcome.inbox.verdicts] == [verdict_at]
    assert outcome.inbox.cap_reports == ()


async def test_the_digest_never_writes_to_a_tracked_issue():
    """`DigestTracker`'s write surfaces raise. This is the module-level half of
    the invariant; the end-to-end half is the `updatedAt` test below."""
    tracker = DigestTracker()
    gate = issue(7, state="human review", updated=T0 + timedelta(hours=1))
    parked = issue(8, state="todo", labels=("status:todo", "status:parked"))
    outcome = await run_digest(tracker, tracker_cfg(), [gate, parked],
                               digest_issue_id="D1", now=T0)
    assert outcome.status == "written"
    assert tracker.body_writes and len(tracker.body_writes) == 1


# --- config ----------------------------------------------------------------------


def _cfg(tmp_path, extra: str = "") -> Config:
    path = tmp_path / "WORKFLOW.md"
    text = WORKFLOW_TMPL.format(ws_root=tmp_path / "ws")
    if extra:
        text = text.replace("polling:", extra + "\npolling:")
    path.write_text(text)
    return Config(load_workflow(path), tmp_path)


def test_the_digest_is_on_by_default(tmp_path):
    """Unlike the fold and review-response features, which gate on an identity
    the orchestrator cannot invent. A digest shipped off would be the
    shipped-but-unwired shape on the one feature whose whole purpose is to stop
    things going unnoticed."""
    assert _cfg(tmp_path).inbox_digest().interval_ms == 86400000


def test_zero_disables_the_digest(tmp_path):
    cfg = _cfg(tmp_path, "inbox_digest:\n  interval_ms: 0")
    assert cfg.inbox_digest().interval_ms == 0


def test_a_negative_interval_is_refused(tmp_path):
    cfg = _cfg(tmp_path, "inbox_digest:\n  interval_ms: -1")
    with pytest.raises(WorkflowError):
        cfg.inbox_digest()


def test_a_wrong_typed_interval_falls_back_to_the_default(tmp_path):
    """The `polling.interval_ms` shape: a malformed value must not stop the
    orchestrator, and the digest is the least dangerous thing to default."""
    cfg = _cfg(tmp_path, 'inbox_digest:\n  interval_ms: "daily"')
    assert cfg.inbox_digest().interval_ms == 86400000


# --- scheduler wiring -------------------------------------------------------------


async def test_a_tick_writes_the_digest_and_moves_no_tracked_issue(
        tmp_path, monkeypatch):
    """THE OBS-022 regression guard.

    The shared `FakeTracker` models GitHub's comment→`updatedAt` echo (and the
    label-write echo), which is exactly the fidelity the 2026-07-02 incident's
    110 passing tests lacked. So the assertion is not "no comment was recorded"
    but "no tracked issue's `updated_at` moved" — a write through ANY surface
    fails it, including one nobody enumerated here.
    """
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    gate = make_issue(7, state="human review")
    parked = make_issue(8, state="todo")
    parked.labels.append("status:parked")
    tracker.candidates = [gate, parked]
    tracker.states = {i.id: i for i in tracker.candidates}
    tracker.active_states = {"todo", "in progress"}
    tracker.repo_open_prs = [
        {"number": 42, "title": "feat: thing", "url": "u42",
         "is_draft": False, "head_ref": "switchboard/issue-7"},
    ]
    tracker.issue_comments["8"] = [
        comment(f"{PARK_COMMENT_PREFIX}implement budget exhausted (2/2 sessions)."),
    ]
    before = {i.id: i.updated_at for i in tracker.candidates}

    await orch._tick()

    assert tracker.digest_issues_created == 1
    assert tracker.body_writes, "the digest never wrote its body"
    written_to, body = tracker.body_writes[-1]
    assert written_to == tracker.digest_issue_id
    assert "#7" in body and "#42" in body
    assert "implement budget exhausted (2/2 sessions)" in body
    # The invariant.
    assert {i.id: i.updated_at for i in tracker.candidates} == before
    assert [c for c in tracker.comments if c[0] in before] == []
    assert [w for w in tracker.labels_added if w[0] in before] == []
    assert [w for w in tracker.body_writes if w[0] in before] == []


async def test_the_digest_is_cadence_gated_not_per_tick(tmp_path, monkeypatch):
    """A ~30s poll interval against a daily digest: the second tick must cost
    zero digest API calls, or the snapshot becomes a per-tick sweep."""
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    tracker.candidates = []
    await orch._tick()
    assert tracker.digest_issue_calls == 1
    calls_after_first = tracker.api_calls

    await orch._tick()
    assert tracker.digest_issue_calls == 1
    assert tracker.api_calls == calls_after_first


async def test_zero_interval_costs_zero_api_calls(tmp_path, monkeypatch):
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    tracker.candidates = []
    monkeypatch.setattr(
        type(orch._cfg), "inbox_digest",
        lambda self: __import__(
            "orchestrator.types", fromlist=["InboxDigestConfig"]
        ).InboxDigestConfig(interval_ms=0))
    await orch._tick()
    assert tracker.digest_issue_calls == 0
    assert tracker.body_writes == []


async def test_a_digest_failure_does_not_break_the_tick(tmp_path, monkeypatch):
    """board_sanity's posture: a report that can halt the poll is a bigger
    hazard than the backlog it enumerates."""
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    tracker.candidates = []
    tracker.digest_issue_error = TrackerError("github_api_status", "502")
    await orch._tick()  # must not raise
    assert tracker.body_writes == []


async def test_an_unexpected_digest_error_does_not_break_the_tick(
        tmp_path, monkeypatch):
    """The half `run_digest` does not wrap is its own rendering. A report is
    non-fatal for the whole report, not just the failures anticipated."""
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    tracker.candidates = []
    monkeypatch.setattr(
        "orchestrator.scheduler.run_digest",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("render blew up")))
    await orch._tick()  # must not raise
    assert tracker.body_writes == []


async def test_a_closed_digest_issue_is_revalidated_and_reopened_elsewhere(
        tmp_path, monkeypatch):
    """The digest issue's own body promises "closing it is safe". A cached id
    that never expires would keep rewriting an issue the operator has already
    filed away."""
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    tracker.candidates = []
    await orch._tick()
    first_id = tracker.digest_issue_id
    tracker.close_digest_issue()

    orch._digest_last_run_at = None  # next tick is due
    await orch._tick()
    assert tracker.digest_issues_created == 2
    assert tracker.digest_issue_id != first_id
    assert tracker.body_writes[-1][0] == tracker.digest_issue_id


async def test_the_digest_resolution_is_single_flighted(tmp_path, monkeypatch):
    """Two concurrent cycles must not each miss the other's create.

    The ops-issue lesson (PR #166, round 5) transplanted: two find-or-creates
    racing produce two inboxes with no network failure involved at all.
    """
    orch, tracker, _runner, _ws = _build_harness(tmp_path, monkeypatch)
    real = tracker.find_or_create_digest_issue

    async def slow_create():
        await asyncio.sleep(0.02)  # widen the race the lock exists to close
        return await real()

    tracker.find_or_create_digest_issue = slow_create
    await asyncio.gather(
        orch._resolve_digest_issue(tracker), orch._resolve_digest_issue(tracker))
    assert tracker.digest_issues_created == 1
