"""Cap-hit post-mortem: the bounded summary pass and its report (issue #16).

The fail-review verifier (#31) reads evidence in four tiers and its tier 4 —
the failed session's own account — was empty by construction. These tests pin
the artifact that fills it, and they are careful about three things that were
live failure modes in the ticket's own analysis:

- **The ceiling must be reached the way production reaches it.** A test that
  set a "cap was hit" flag would pass while the feature could never fire: the
  budget branch compares ACCUMULATED `cost_usd` against the configured ceiling
  and needs a live session id to resume, so the fake accumulates real cost and
  returns real session ids.
- **The budget path expects the summary pass to FAIL.** A session that has spent
  its cost ceiling cannot run inference to report its own death. If the
  fallback were only exercised on a contrived error, the one path where it is
  load-bearing would be untested.
- **Disagreement must survive to the comment.** The report's consumer is a
  verifier weighing tier 4 against tiers 1-3; a report that silently resolved
  the two classes would delete the signal that consumer exists to weigh.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.cap_report import (
    BUDGET_CAP,
    CAP_REPORT_HEADING,
    CAP_SUMMARY_PROMPT,
    TURNS_CAP,
    UNAVAILABLE,
    UNPARSED,
    MechanicalFacts,
    SelfReport,
    parse_self_report,
    render_report,
)
from orchestrator.failure_taxonomy import CapFailureClass
from orchestrator.types import TrackerError, TurnResult

from test_integration import (  # the shared fakes; this suite adds no second set
    WORKFLOW_TMPL,
    _build_harness,
    make_issue,
    wait_for,
)


# --- fakes --------------------------------------------------------------------


class CapRunner:
    """A runner that reaches a ceiling by SPENDING, not by declaring.

    `cost_usd` per turn and the summary pass's own outcome are the only knobs;
    everything the scheduler branches on (the accumulated cost crossing the
    configured ceiling, a live session id to resume) is derived, per
    METHODOLOGY.md's fake-fidelity rule.
    """

    provider_id = "claude"

    def __init__(self, cost_per_turn: float = 0.04, summary: TurnResult | None = None):
        self.turn_timeout_ms = 5000
        self.stall_timeout_ms = 0
        self.max_budget_usd: float | None = None
        self.cost_per_turn = cost_per_turn
        self.summary = summary
        self.turns: list[tuple[str, str | None, str]] = []
        self.summary_calls: list[tuple[Path, str, str]] = []

    async def run_turn(self, workspace, prompt, resume_session_id, on_event,
                       issue_id, agent_token=None):
        self.turns.append((issue_id, resume_session_id, prompt))
        return TurnResult(status="succeeded", session_id=f"sess-{len(self.turns)}",
                          cost_usd=self.cost_per_turn, num_turns=1)

    async def run_summary_turn(self, workspace, prompt, resume_session_id,
                               on_event, issue_id, agent_token=None):
        self.summary_calls.append((workspace, prompt, resume_session_id))
        if self.summary is None:
            # The budget path's expected case: a budget-dead session cannot
            # afford the turn that would report its own death.
            return TurnResult(status="failed", session_id=resume_session_id,
                              error="credit_balance_too_low")
        return self.summary


class ToollessCodexRunner(CapRunner):
    """A provider with no summary pass at all — codex today (issue #181)."""

    provider_id = "codex"
    run_summary_turn = None  # type: ignore[assignment]


def _summary(text: str) -> TurnResult:
    return TurnResult(status="succeeded", session_id="sess-1", cost_usd=0.02,
                      text=text)


BUDGET_TMPL = WORKFLOW_TMPL.replace(
    """claude:
  command: "unused-by-fake-runner"
  max_turns: 1""",
    """claude:
  command: "unused-by-fake-runner"
  max_budget_usd: 0.05
  max_turns: 1""",
).replace("max_turns: 1\n  max_retry_backoff_ms", "max_turns: 4\n  max_retry_backoff_ms")

# The turn ceiling, reached with no budget configured at all: `max_turns: 2` and
# a runner that never crosses a (nonexistent) cost ceiling.
TURNS_TMPL = WORKFLOW_TMPL.replace(
    "max_turns: 1\n  max_retry_backoff_ms", "max_turns: 2\n  max_retry_backoff_ms")


async def _run_to_cap(tmp_path, monkeypatch, runner, tmpl=BUDGET_TMPL):
    orch, tracker, _, ws_root = _build_harness(
        tmp_path, monkeypatch, tmpl, runner=runner)
    issue = make_issue(1, "todo")
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    await orch._tick()
    await wait_for(lambda: not orch.running)
    return orch, tracker, issue


def _report_comment(tracker) -> str:
    bodies = [b for _, b in tracker.comments if b.startswith(CAP_REPORT_HEADING)]
    assert len(bodies) == 1, f"expected exactly one cap-hit report, got {len(bodies)}"
    return bodies[0]


def _yaml_of(body: str) -> dict:
    block = body.split("```yaml\n", 1)[1].split("\n```", 1)[0]
    return yaml.safe_load(block)


# --- AC 1: the budget ceiling posts a report -----------------------------------


async def test_a_budget_cap_hit_runs_the_summary_pass_and_posts_a_report(
        tmp_path, monkeypatch):
    """AC1. The ceiling is reached by ACCUMULATING cost past the configured
    budget — 2 turns x $0.04 crosses $0.05 — and the pass resumes the live
    session id the turn that crossed it returned."""
    runner = CapRunner(summary=_summary(
        "CLASS: complexity\n"
        "ATTEMPTED: wire the report into the turn loop\n"
        "FURTHEST: tests written, integration unwired\n"
        "NEXT: split the ticket"))

    orch, tracker, _ = await _run_to_cap(tmp_path, monkeypatch, runner)

    # The cap was reached by spending, not by declaring.
    assert len(runner.turns) == 2
    assert runner.max_budget_usd == 0.05
    # ...and the pass resumed the session id that the last turn returned.
    assert [c[2] for c in runner.summary_calls] == ["sess-2"]
    assert runner.summary_calls[0][1] == CAP_SUMMARY_PROMPT

    body = _report_comment(tracker)
    data = _yaml_of(body)
    assert data["cap"] == BUDGET_CAP
    assert data["self_reported"] == CapFailureClass.COMPLEXITY.value
    assert data["mechanical"] == CapFailureClass.QUOTA.value
    assert data["turns_spent"] == 2
    assert data["cost_usd"] == pytest.approx(0.08)
    assert data["budget_usd"] == 0.05
    assert "wire the report into the turn loop" in body


async def test_the_yaml_class_values_come_from_the_taxonomy_module(
        tmp_path, monkeypatch):
    """AC1. #169 shipped the class set as an importable contract precisely so a
    consumer could not drift from it; this asserts the rendered wire format is
    that contract's values and not a prose copy of them."""
    runner = CapRunner(summary=_summary("CLASS: iteration"))
    _, tracker, _ = await _run_to_cap(tmp_path, monkeypatch, runner)

    data = _yaml_of(_report_comment(tracker))
    values = {c.value for c in CapFailureClass}
    assert data["self_reported"] in values
    assert data["mechanical"] in values
    # The prompt derives its menu from the enum too — every class, verbatim.
    for member in CapFailureClass:
        assert member.value in CAP_SUMMARY_PROMPT


async def test_the_turn_ceiling_reports_too_and_classes_it_differently(
        tmp_path, monkeypatch):
    """AC1's other ceiling. The mechanical class is the one thing the
    orchestrator can honestly conclude from the break: turns exhausted reads as
    `iteration`, budget exhausted as `quota`."""
    runner = CapRunner(cost_per_turn=0.0, summary=_summary("CLASS: quota"))
    _, tracker, _ = await _run_to_cap(
        tmp_path, monkeypatch, runner, tmpl=TURNS_TMPL)

    data = _yaml_of(_report_comment(tracker))
    assert data["cap"] == TURNS_CAP
    assert data["mechanical"] == CapFailureClass.ITERATION.value
    assert data["budget_usd"] is None


# --- AC 2: the mechanical fallback --------------------------------------------


async def test_a_failed_summary_pass_degrades_to_the_mechanical_fallback(
        tmp_path, monkeypatch):
    """AC2, on the path where it is load-bearing: the DEFAULT `CapRunner`
    summary outcome is failure, because that is what a budget-dead session
    actually does. The comment is posted anyway and carries the facts held at
    the break."""
    runner = CapRunner()  # summary pass fails — the expected budget-path case

    _, tracker, _ = await _run_to_cap(tmp_path, monkeypatch, runner)

    body = _report_comment(tracker)
    data = _yaml_of(body)
    assert data["self_reported"] == UNAVAILABLE
    assert data["mechanical"] == CapFailureClass.QUOTA.value
    assert data["agreement"] == UNAVAILABLE
    assert data["turns_spent"] == 2 and data["max_turns"] == 4
    assert data["cost_usd"] == pytest.approx(0.08)
    assert "credit_balance_too_low" in body
    assert "cannot run inference to report its own death" in body


async def test_a_provider_with_no_summary_pass_still_gets_a_report(
        tmp_path, monkeypatch):
    """AC2. Codex has no budget ceiling (#181) but does have a turn ceiling, and
    a provider that cannot self-report must not silently post nothing."""
    runner = ToollessCodexRunner(cost_per_turn=0.0)
    _, tracker, _ = await _run_to_cap(
        tmp_path, monkeypatch, runner, tmpl=TURNS_TMPL)

    body = _report_comment(tracker)
    assert _yaml_of(body)["self_reported"] == UNAVAILABLE
    assert "has no summary pass" in body


async def test_a_summary_pass_that_raises_does_not_fail_the_worker(
        tmp_path, monkeypatch, capfd):
    """AC2. The feature is purely additive at this boundary: before it, a
    cap-exhausted session logged `outcome="completed"`. A raising diagnostic
    must not convert that into a FAILED session and re-enter retry/circuit
    accounting — that would make a post-mortem a routing change, which is #31's
    job and not this one's."""
    runner = CapRunner()

    async def boom(*a, **kw):
        raise RuntimeError("provider exploded")

    runner.run_summary_turn = boom
    orch, tracker, _ = await _run_to_cap(tmp_path, monkeypatch, runner)

    assert "outcome=completed" in capfd.readouterr().err
    body = _report_comment(tracker)
    assert "provider exploded" in body
    assert _yaml_of(body)["self_reported"] == UNAVAILABLE


async def test_a_tracker_failure_ends_the_session_normally(
        tmp_path, monkeypatch, capfd):
    """AC2's floor. If even the comment write fails there is nothing left to
    try — but the session must still end the way it ended before this feature
    existed."""
    runner = CapRunner(summary=_summary("CLASS: quota"))
    orch, tracker, _, _ = _build_harness(
        tmp_path, monkeypatch, BUDGET_TMPL, runner=runner)

    async def refuse(issue_id, body):
        raise TrackerError("comment write failed")

    tracker.add_issue_comment = refuse
    issue = make_issue(1, "todo")
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    await orch._tick()
    await wait_for(lambda: not orch.running)

    err = capfd.readouterr().err
    assert "cap-hit report failed; session ends unreported" in err
    assert "outcome=completed" in err


async def test_no_report_when_the_loop_ends_for_any_other_reason(
        tmp_path, monkeypatch):
    """The negative space. A role-pin state change ends the session at the turn
    boundary too, and that is not a cap-hit — reporting one would put a
    post-mortem on a session that is merely being re-dispatched in a new role."""
    runner = CapRunner(cost_per_turn=0.0, summary=_summary("CLASS: quota"))
    orch, tracker, _, _ = _build_harness(
        tmp_path, monkeypatch, TURNS_TMPL, runner=runner)
    issue = make_issue(1, "todo")
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    await orch._tick()
    # The state changes under the running session: role-pin break, not a cap.
    tracker.states = {"node-1": make_issue(1, "human review")}
    await wait_for(lambda: not orch.running)

    assert runner.summary_calls == []
    assert [b for _, b in tracker.comments
            if b.startswith(CAP_REPORT_HEADING)] == []


# --- AC 3: two claims, and the disagreement between them ----------------------


def test_the_block_carries_both_claims_and_renders_their_disagreement():
    """AC3. `self_reported` and `mechanical` are separate fields, and when they
    differ the report says so in prose a human reads AND in a field a parser
    reads. Neither one is rewritten to match the other."""
    facts = MechanicalFacts(cap=BUDGET_CAP, turns_spent=3, max_turns=3,
                            cost_usd=1.5, budget_usd=1.0, branch="issue-16",
                            commits_ahead=2, last_event="turn_completed",
                            last_event_at="2026-08-29T12:00:00+00:00")
    report = SelfReport(cap_class=CapFailureClass.COMPLEXITY,
                        attempted="a", furthest="b", next_action="c")

    body = render_report(facts, report)
    data = _yaml_of(body)

    assert data["self_reported"] == CapFailureClass.COMPLEXITY.value
    assert data["mechanical"] == CapFailureClass.QUOTA.value
    assert data["agreement"] == "disagree"
    assert "**Disagreement.**" in body
    assert "neither overrides the other" in body


def test_agreement_is_recorded_when_the_two_claims_match():
    facts = MechanicalFacts(cap=TURNS_CAP, turns_spent=3, max_turns=3,
                            cost_usd=0.5, budget_usd=None)
    report = SelfReport(cap_class=CapFailureClass.ITERATION, attempted="a")

    body = render_report(facts, report)
    assert _yaml_of(body)["agreement"] == "agree"
    assert "**Disagreement.**" not in body


def test_an_unknown_class_is_recorded_as_unparsed_not_mapped_to_a_neighbour():
    """AC3's edge. A class outside the taxonomy is a fact about the session, not
    a parse to repair: mapping it to the nearest member would manufacture
    agreement the session never expressed."""
    report = parse_self_report("CLASS: ran-out-of-ideas\nATTEMPTED: things")

    assert report.cap_class is None
    assert report.class_token == "ran-out-of-ideas"
    assert report.class_field == UNPARSED

    facts = MechanicalFacts(cap=BUDGET_CAP, turns_spent=1, max_turns=1,
                            cost_usd=0.1, budget_usd=0.05)
    body = render_report(facts, report)
    assert _yaml_of(body)["self_reported"] == UNPARSED
    assert _yaml_of(body)["agreement"] == UNAVAILABLE
    assert "ran-out-of-ideas" in body  # the raw claim survives into the prose


# --- parsing and rendering ----------------------------------------------------


def test_the_four_field_shape_parses_including_around_model_chatter():
    report = parse_self_report(
        "Sure, here is my report.\n\n"
        "CLASS: `iteration`\n"
        "ATTEMPTED: refactor the scheduler turn loop\n"
        "FURTHEST: got the loop split but tests red\n"
        "NEXT: revert and take the smaller cut\n"
    )

    assert report.cap_class is CapFailureClass.ITERATION
    assert report.attempted == "refactor the scheduler turn loop"
    assert report.furthest == "got the loop split but tests red"
    assert report.next_action == "revert and take the smaller cut"


def test_a_partial_report_is_still_evidence():
    """Prose with no class beats no account at all: this is the only self-report
    that will ever exist for this session."""
    report = parse_self_report("FURTHEST: the migration script half-written")

    assert report.present
    assert report.cap_class is None
    assert report.class_field == UNPARSED
    assert "half-written" in render_report(
        MechanicalFacts(cap=TURNS_CAP, turns_spent=1, max_turns=1,
                        cost_usd=0.0, budget_usd=None),
        report,
    )


def test_an_empty_pass_yields_an_empty_report_and_the_fallback_prose():
    report = parse_self_report("")
    assert not report.present
    assert report.class_field == UNAVAILABLE

    body = render_report(
        MechanicalFacts(cap=BUDGET_CAP, turns_spent=2, max_turns=5,
                        cost_usd=1.0, budget_usd=1.0),
        report,
        summary_error="turn_timeout",
    )
    assert body.startswith(CAP_REPORT_HEADING)
    assert "unavailable" in body and "turn_timeout" in body


def test_the_block_is_parseable_yaml_even_with_an_awkward_branch_name():
    """#12 parses this block. A branch name carrying a colon would break a
    naively rendered scalar and take the dashboard's parse down with it."""
    facts = MechanicalFacts(cap=TURNS_CAP, turns_spent=1, max_turns=1,
                            cost_usd=0.0, budget_usd=None,
                            branch="wip: no really", commits_ahead=None)
    data = _yaml_of(render_report(facts, SelfReport()))

    assert data["branch"] == "wip: no really"
    assert data["commits_ahead"] is None
    assert data["last_event"] == ""
