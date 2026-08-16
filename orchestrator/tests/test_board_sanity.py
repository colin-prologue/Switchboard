"""Board-state sanity check (issue #52) — detect, never revert.

Every config here comes from a REAL shipped template (`workflow/WORKFLOW.base.md`
and `workflow/stances/WORKFLOW.prototype.md`), never a hand-built TrackerConfig.
A hand-built config would let this suite agree with itself about what `base`
and `prototype` legitimately run, which is exactly the claim under test: a
check that flags one stance's normal operation is worse than no check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.board_sanity import (
    CONDITION_MULTIPLE_LIVE,
    CONDITION_TERMINAL_WHILE_OPEN,
    CONDITION_UNDEFINED,
    MARKER,
    find_invalid_states,
    report_board_state,
)
from orchestrator.types import Issue, IssueComment
from orchestrator.workflow import Config, load_workflow

REPO_ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path, template: Path) -> Config:
    """A shipped template with register-project.sh's substitutions applied."""
    substituted = (
        template.read_text(encoding="utf-8")
        .replace("{{REPO}}", "acme/widgets")
        .replace("{{WORKSPACE_ROOT}}", "/tmp/ws")
        .replace("{{MAX_AGENTS}}", "10")
        .replace("{{CONVENTION_ROOT}}", "")
        .replace("{{BASE_BRANCH}}", "main")
        .replace("{{VERIFY_CMD}}", "true")
        .replace("{{VERIFY_TOOLS}}", "")
        .replace("{{REVIEW_BOT}}", "")
        .replace("{{REVIEW_BOT_YAML}}", "")
    )
    path = tmp_path / "WORKFLOW.md"
    path.write_text(substituted)
    return Config(load_workflow(path), tmp_path)


@pytest.fixture
def base_cfg(tmp_path: Path):
    return _config(tmp_path, REPO_ROOT / "workflow" / "WORKFLOW.base.md").tracker()


@pytest.fixture
def prototype_cfg(tmp_path: Path):
    return _config(
        tmp_path, REPO_ROOT / "workflow" / "stances" / "WORKFLOW.prototype.md"
    ).tracker()


def issue(*labels: str, number: str = "7", state: str = "todo") -> Issue:
    return Issue(
        id=f"I_{number}",
        identifier=number,
        title="t",
        description=None,
        priority=None,
        state=state,
        branch_name=None,
        url=None,
        labels=list(labels),
    )


class SanityTracker:
    """Fake tracker for the detection path ONLY.

    Every status-label write surface raises: detection must never write a
    `status:*` label, and the way to assert that is to make the write
    impossible rather than to inspect a call log afterwards.
    """

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []
        self.issue_comments: dict[str, list[IssueComment]] = {}
        self.comment_fetches = 0
        self.fetch_error: Exception | None = None

    async def fetch_issue_comments(self, issue_number):
        self.comment_fetches += 1
        if self.fetch_error is not None:
            raise self.fetch_error
        return list(self.issue_comments.get(str(issue_number), []))

    async def add_issue_comment(self, issue_id, body):
        self.comments.append((issue_id, body))
        # Mimic GitHub: the comment is visible to every later fetch. Without
        # this the durable half of the dedupe would be untestable.
        number = issue_id.removeprefix("I_")
        self.issue_comments.setdefault(number, []).append(
            IssueComment(id=f"C{len(self.comments)}", body=body, login="bot")
        )

    async def add_labels(self, issue_id, label_names):
        raise AssertionError(f"detection wrote labels: {label_names}")

    async def remove_labels(self, issue_id, label_names):
        raise AssertionError(f"detection removed labels: {label_names}")

    async def set_sole_status_label(self, issue_id, label, **kw):
        raise AssertionError(f"detection swapped a status label: {label}")


# --- the three conditions -----------------------------------------------------


def test_undefined_status_label_is_detected(base_cfg):
    findings = find_invalid_states(issue("status:frobnicate"), base_cfg)
    assert [f.condition for f in findings] == [CONDITION_UNDEFINED]
    assert findings[0].labels == ("status:frobnicate",)


def test_multiple_live_status_labels_are_detected(base_cfg):
    findings = find_invalid_states(issue("status:todo", "status:triage"), base_cfg)
    assert [f.condition for f in findings] == [CONDITION_MULTIPLE_LIVE]
    assert findings[0].labels == ("status:todo", "status:triage")


def test_terminal_state_while_open_is_detected(base_cfg):
    findings = find_invalid_states(issue("status:closed"), base_cfg)
    assert [f.condition for f in findings] == [CONDITION_TERMINAL_WHILE_OPEN]


def test_parked_coexists_with_a_live_status_label(base_cfg):
    """`status:parked` is a durable marker, not a second claim on the state."""
    assert find_invalid_states(issue("status:todo", "status:parked"), base_cfg) == []


def test_conditions_are_reported_independently(base_cfg):
    findings = find_invalid_states(
        issue("status:todo", "status:frobnicate"), base_cfg)
    assert sorted(f.condition for f in findings) == [
        CONDITION_MULTIPLE_LIVE, CONDITION_UNDEFINED]


# --- no false positives on either stance's own legitimate states --------------


@pytest.mark.parametrize(
    "labels",
    [
        ("status:triage",), ("status:todo",), ("status:in-progress",),
        ("status:drafting",), ("status:plan-review",), ("status:decision",),
        ("status:blocked",), ("status:human-review",), ("status:parked",),
        ("status:in-progress", "status:parked"),
    ],
)
def test_base_stance_legitimate_states_are_clean(base_cfg, labels):
    assert find_invalid_states(issue(*labels), base_cfg) == []


@pytest.mark.parametrize(
    "labels",
    [
        ("status:todo",), ("status:in-progress",), ("status:review",),
        ("status:human-review",), ("status:parked",),
        ("status:todo", "status:parked"),
    ],
)
def test_prototype_stance_legitimate_states_are_clean(prototype_cfg, labels):
    assert find_invalid_states(issue(*labels), prototype_cfg) == []


def test_the_two_stances_really_do_disagree(base_cfg, prototype_cfg):
    """Discriminator: without this, both suites above could be passing because
    the check is judging one shared list rather than each project's own."""
    assert find_invalid_states(issue("status:review"), base_cfg)
    assert find_invalid_states(issue("status:triage"), prototype_cfg)


# --- the reporting path -------------------------------------------------------


async def _report(issues, cfg, tracker, reported=None):
    return await report_board_state(
        issues, cfg, tracker, reported=reported if reported is not None else set())


@pytest.mark.asyncio
async def test_report_logs_and_comments_without_writing_a_status_label(
    base_cfg, monkeypatch
):
    records: list[tuple[str, dict]] = []
    monkeypatch.setattr("orchestrator.board_sanity.log",
                        lambda msg, **ctx: records.append((msg, ctx)))
    tracker = SanityTracker()

    findings = await _report([issue("status:frobnicate")], base_cfg, tracker)

    assert [f.condition for f in findings] == [CONDITION_UNDEFINED]
    assert len(records) == 1
    msg, ctx = records[0]
    assert "BOARD STATE INVALID" in msg
    assert ctx["issue_identifier"] == "7"
    assert ctx["condition"] == CONDITION_UNDEFINED
    assert ctx["labels"] == "status:frobnicate"
    assert len(tracker.comments) == 1
    body = tracker.comments[0][1]
    assert MARKER.format(condition=CONDITION_UNDEFINED) in body
    assert "status:frobnicate" in body
    # SanityTracker raises on every label write, so reaching here IS the
    # never-writes assertion.


@pytest.mark.asyncio
async def test_valid_issue_produces_no_log_and_no_comment(base_cfg, monkeypatch):
    records: list[str] = []
    monkeypatch.setattr("orchestrator.board_sanity.log",
                        lambda msg, **ctx: records.append(msg))
    tracker = SanityTracker()

    assert await _report([issue("status:todo")], base_cfg, tracker) == []
    assert records == []
    assert tracker.comments == []
    assert tracker.comment_fetches == 0


@pytest.mark.asyncio
async def test_second_tick_over_unchanged_state_posts_nothing(base_cfg):
    tracker = SanityTracker()
    reported: set[tuple[str, str]] = set()
    issues = [issue("status:frobnicate")]

    await _report(issues, base_cfg, tracker, reported)
    await _report(issues, base_cfg, tracker, reported)

    assert len(tracker.comments) == 1
    assert tracker.comment_fetches == 1  # the memo short-circuits before the API


@pytest.mark.asyncio
async def test_restart_does_not_repost_because_the_marker_is_durable(base_cfg):
    """The memo is in-process; the comment is not. A fresh memo (restart) must
    find the marker and stay quiet."""
    tracker = SanityTracker()
    issues = [issue("status:frobnicate")]

    await _report(issues, base_cfg, tracker, set())
    await _report(issues, base_cfg, tracker, set())  # "restart"

    assert len(tracker.comments) == 1
    assert tracker.comment_fetches == 2


@pytest.mark.asyncio
async def test_malformed_label_set_logs_and_continues(base_cfg, monkeypatch):
    records: list[str] = []
    monkeypatch.setattr("orchestrator.board_sanity.log",
                        lambda msg, **ctx: records.append(msg))
    broken = issue("status:todo", number="9")
    broken.labels = None  # unreadable label set
    tracker = SanityTracker()

    findings = await _report(
        [broken, issue("status:frobnicate")], base_cfg, tracker)

    assert [f.condition for f in findings] == [CONDITION_UNDEFINED]
    assert any("could not read this issue's labels" in m for m in records)
    assert len(tracker.comments) == 1  # the healthy issue was still reported


@pytest.mark.asyncio
async def test_comment_failure_is_non_fatal(base_cfg, monkeypatch):
    records: list[str] = []
    monkeypatch.setattr("orchestrator.board_sanity.log",
                        lambda msg, **ctx: records.append(msg))
    tracker = SanityTracker()
    tracker.fetch_error = RuntimeError("github is down")

    findings = await _report([issue("status:frobnicate")], base_cfg, tracker)

    assert [f.condition for f in findings] == [CONDITION_UNDEFINED]
    assert any("could not post the report comment" in m for m in records)
