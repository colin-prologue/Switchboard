"""Fail-review verifier: park-time dispatch, classify, route recovery (issue #31).

The post-failure twin of triage. On an IMPLEMENT-role cap-hit the orchestrator
relabels `status:fail-review`, writes the durable `gate:fail-reviewed` episode
bound, clears the implement + fail-review counters, and lets the next poll tick
dispatch exactly one verifier session. The verifier posts a `## Fail-review
verdict` comment and routes recovery with `gh issue edit` — no orchestrator-side
routing, and therefore no machine-readable verdict channel.

What these tests are careful about, because each was a live failure mode in the
ticket's own analysis:

- **The counter must already be exhausted.** A fresh-counter test would pass
  while the feature could never fire, since an implement cap-hit arrives with the
  implement counter spent and a shared cap would park on the very next dispatch.
- **The reset is keyed on the RELABEL, not on observing the routed state.** An
  observation-keyed reset strands every `drafting` route: those issues leave
  `active_states` and are never polled again.
- **Park must never strand an issue on unpark.** An issue stripped to
  `[status:parked]` alone derives `"none"` the moment the operator removes that
  label — invisible to the candidate poll, on the one recovery action the park
  comment documents.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

import orchestrator.scheduler as scheduler_mod
from orchestrator.scheduler import (
    FAIL_REVIEW_LABEL,
    FAIL_REVIEW_MARKER,
    FAIL_REVIEW_ROLE,
    HUMAN_REVIEW_LABEL,
    IMPLEMENT_ROLE,
    IN_PROGRESS_LABEL,
    PARK_LABEL,
    PARK_STRIP_LABELS,
    TODO_LABEL,
    VERIFY_ROLE,
    VERIFY_STATES,
    session_role,
)
from orchestrator.tracker import normalize_status_state
from orchestrator.types import TrackerError
from orchestrator.workflow import Config, load_workflow

from test_integration import (  # the shared fakes; this suite adds no second set
    RR_BOT,
    RR_SELF,
    WORKFLOW_TMPL,
    _bind_pr,
    _build_harness,
    _enable_app_identity,
    make_issue,
    rr_issue,
    wait_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TRIAGE_MARKER = "gate:triage-passed"

# `fail review` must be ACTIVE or nothing dispatches the verifier — the exact
# trap METHODOLOGY.md's "Writing a stance" list opens with.
FAIL_REVIEW_TMPL = WORKFLOW_TMPL.replace(
    'active_states: ["todo", "in progress"]',
    'active_states: ["todo", "in progress", "fail review"]',
)


def _status_labels(issue) -> list[str]:
    return sorted(l for l in issue.labels if l.startswith("status:"))


def _fail_review_harness(tmp_path, monkeypatch, *, tmpl=FAIL_REVIEW_TMPL):
    """Harness whose issue has ALREADY SPENT its implement budget.

    Seeding the counter is the point: see the module docstring. `states` and
    `candidates` hold the same object so the two fetch paths agree.
    """
    orch, tracker, runner, ws_root = _build_harness(tmp_path, monkeypatch, tmpl)
    issue = make_issue(1, "todo")
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    cap = orch._cfg.agent().max_sessions_per_issue
    orch.sessions_per_issue[("node-1", IMPLEMENT_ROLE)] = cap
    return orch, tracker, runner, issue, ws_root


def _route(tracker, issue, *, remove, add):
    """Apply a verdict route the way the prompt mode's `gh issue edit` does.

    The routing writes OUT of fail-review are made by the VERIFIER SESSION, not
    the tracker layer — so the test performs them as plain label edits, with no
    preemption guard and no `expected_status`, exactly as triage routes today.
    """
    async def go():
        await tracker.remove_labels(issue.id, list(remove))
        await tracker.add_labels(issue.id, list(add))
    return go()


# --- roles and caps -----------------------------------------------------------


def test_fail_review_is_its_own_role_and_verify_states_is_unchanged():
    """A THIRD role, deliberately not a member of VERIFY_STATES: sharing
    triage's counter would arrive at fail-review with the verify budget already
    spent whenever triage used its cap."""
    assert session_role("fail review") == FAIL_REVIEW_ROLE
    assert FAIL_REVIEW_ROLE not in (VERIFY_ROLE, IMPLEMENT_ROLE)
    assert FAIL_REVIEW_ROLE not in VERIFY_STATES
    assert VERIFY_STATES == {"triage"}
    # ...and the other two roles still resolve as they did.
    assert session_role("triage") == VERIFY_ROLE
    assert session_role("todo") == IMPLEMENT_ROLE
    assert session_role("in progress") == IMPLEMENT_ROLE


def test_fail_review_role_literal_carries_a_space():
    """One value serves two lookups: the derived state string for
    `status:fail-review` AND the role name interpolated into the park reason's
    operator-facing prose ("fail review budget exhausted")."""
    assert FAIL_REVIEW_ROLE == "fail review"
    assert normalize_status_state([FAIL_REVIEW_LABEL], closed=False) \
        == FAIL_REVIEW_ROLE


@pytest.mark.parametrize("raw, expected", [
    (None, 1),          # key absent -> the default
    (2, 2),             # a valid override is honored
    (True, 1),          # bool is an int in Python; coerce (the classic trap)
    (False, 1),
    (0, 1),             # <= 0 would disable parking; parking is always on
    (-3, 1),
    ("2", 1),           # non-int
    (1.5, 1),
])
def test_fail_review_cap_defaults_to_one_and_coerces_invalid_values(
        tmp_path, raw, expected):
    """Same always-on coercion `max_sessions_per_issue` gets, different default
    — the verifier is one diagnostic pass, not an implementation budget."""
    line = "" if raw is None else \
        f"\n  max_fail_review_sessions_per_issue: {raw!r}"
    p = tmp_path / "WORKFLOW.md"
    p.write_text(
        FAIL_REVIEW_TMPL.format(ws_root=tmp_path / "ws").replace(
            "  max_sessions_per_issue: 2",
            f"  max_sessions_per_issue: 2{line}"),
        encoding="utf-8")
    assert Config(load_workflow(p), tmp_path) \
        .agent().max_fail_review_sessions_per_issue == expected


def test_cap_lookup_is_role_keyed(tmp_path, monkeypatch):
    """Pre-#31 the COUNTER was per-role but the CAP was one global scalar."""
    orch, *_ = _build_harness(tmp_path, monkeypatch, FAIL_REVIEW_TMPL)
    assert orch._cap_for_role(IMPLEMENT_ROLE) == 2      # WORKFLOW_TMPL's value
    assert orch._cap_for_role(VERIFY_ROLE) == 2
    assert orch._cap_for_role(FAIL_REVIEW_ROLE) == 1


def test_reset_defaults_to_every_role_and_accepts_a_subset(tmp_path, monkeypatch):
    """The default is what the two pre-#31 callers want (unpark, and the
    review-response trigger whose own comment requires "Reset every role"); the
    cap branch is the third caller and must be selective, so a ticket that spent
    its triage budget does not silently get it back on an implementation
    failure."""
    orch, *_ = _build_harness(tmp_path, monkeypatch, FAIL_REVIEW_TMPL)
    seed = {
        ("node-1", VERIFY_ROLE): 1,
        ("node-1", IMPLEMENT_ROLE): 2,
        ("node-1", FAIL_REVIEW_ROLE): 1,
        ("node-2", IMPLEMENT_ROLE): 1,     # a different issue is never touched
    }
    orch.sessions_per_issue.update(seed)
    orch._reset_issue_sessions("node-1", roles=(IMPLEMENT_ROLE, FAIL_REVIEW_ROLE))
    assert orch.sessions_per_issue == {
        ("node-1", VERIFY_ROLE): 1, ("node-2", IMPLEMENT_ROLE): 1}

    orch.sessions_per_issue.clear()
    orch.sessions_per_issue.update(seed)
    orch._reset_issue_sessions("node-1")                # default: every role
    assert orch.sessions_per_issue == {("node-2", IMPLEMENT_ROLE): 1}


# --- the end-to-end episode ---------------------------------------------------


async def test_end_to_end_from_an_exhausted_counter(tmp_path, monkeypatch):
    """AC: cap-hit relabels; exactly ONE fail-review session dispatches on a
    fresh role counter; a retry-class verdict routes to `todo`; the implement
    counter is cleared; the next tick dispatches an implement session.

    A fresh-counter test does not satisfy this — see the module docstring.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    # --- tick 1: the cap branch. Writes, THEN reset; refuses and yields. -----
    await orch._tick()
    assert tracker.sole_status_swaps == [("node-1", FAIL_REVIEW_LABEL)]
    assert _status_labels(issue) == [FAIL_REVIEW_LABEL]   # prior status removed
    assert FAIL_REVIEW_MARKER in issue.labels             # durable episode bound
    assert TRIAGE_MARKER in issue.labels                  # survives the round trip
    assert issue.state == FAIL_REVIEW_ROLE
    assert orch.sessions_for_issue("node-1") == {}        # episode start: cleared
    assert "node-1" not in orch.claimed                   # released before REFUSED
    assert "node-1" not in orch.parked                    # NOT parked: diagnosed
    assert runner.turns == []                             # nothing dispatched yet

    # --- tick 2: exactly one verifier, on a FRESH fail-review counter. -------
    # `hold` freezes the verifier mid-turn. That is not decoration: on a
    # successful turn the scheduler arms a CONTINUATION retry, and a session
    # left to complete unattended re-dispatches on the budget the cap branch
    # just reset — spending it before the test can route a verdict. Holding,
    # then quiescing the candidate list before release, is how the other
    # role-transition tests in this repo keep the timeline theirs.
    runner.hold = True
    await orch._tick()
    assert orch.sessions_for_issue("node-1") == {FAIL_REVIEW_ROLE: 1}
    await wait_for(lambda: len(runner.turns) == 1)
    assert "node-1" in orch.running
    # The verifier writes NO claim label: `_apply_in_progress_label` gates on
    # `todo`, so a fail-review dispatch leaves the board reading `fail review`.
    assert _status_labels(issue) == [FAIL_REVIEW_LABEL]

    tracker.candidates = []                  # quiesce the continuation retry
    runner.release.set()
    await wait_for(lambda: not orch.running)

    # --- the verdict: a retry-class route, written by the SESSION. -----------
    await _route(tracker, issue,
                 remove=[FAIL_REVIEW_LABEL], add=[TODO_LABEL])
    assert issue.state == "todo"
    assert FAIL_REVIEW_MARKER in issue.labels    # todo route RETAINS both markers
    assert TRIAGE_MARKER in issue.labels         # or dispatch would refuse it

    # --- tick 3: an implement session dispatches on the cleared counter. -----
    await wait_for(lambda: "node-1" not in orch.claimed)   # verifier torn down
    runner.release.clear()
    runner.hold = True
    tracker.candidates = [issue]
    await orch._tick()
    # The IMPLEMENT budget is fresh (1, not the seeded cap) and the fail-review
    # spend is NOT refunded — the episode-start reset cleared both counters, and
    # the verifier then booked its own.
    assert orch.sessions_for_issue("node-1") == {FAIL_REVIEW_ROLE: 1,
                                                IMPLEMENT_ROLE: 1}
    assert "node-1" not in orch.parked
    tracker.candidates = []
    runner.release.set()
    await wait_for(lambda: not orch.running)


async def test_drafting_route_resets_without_an_intervening_restart(
        tmp_path, monkeypatch):
    """AC: the `iteration` verdict routes to `drafting`, which LEAVES
    `active_states` and is never polled again — so an observation-keyed reset
    would strand it while still passing the `todo`-route criterion above. Assert
    the counters were cleared at the cap-hit relabel, with no restart."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    await orch._tick()                                   # cap branch
    assert orch.sessions_for_issue("node-1") == {}        # cleared AT THE RELABEL

    runner.hold = True                # freeze the verifier; see the todo-route
    await orch._tick()                # test for why the continuation must not run
    await wait_for(lambda: len(runner.turns) == 1)
    assert orch.sessions_for_issue("node-1") == {FAIL_REVIEW_ROLE: 1}
    tracker.candidates = []                  # quiesce the continuation retry
    runner.release.set()
    await wait_for(lambda: not orch.running)

    # `iteration` -> drafting, clearing BOTH markers (pinned flags).
    await _route(tracker, issue,
                 remove=[FAIL_REVIEW_LABEL, FAIL_REVIEW_MARKER, TRIAGE_MARKER],
                 add=["status:drafting"])
    assert issue.state == "drafting"
    assert FAIL_REVIEW_MARKER not in issue.labels
    assert TRIAGE_MARKER not in issue.labels

    # The human re-drafts and triage re-passes. The scheduler never observed the
    # drafting state — and the implement counter is nonetheless clear, so the
    # FIRST tick after re-pass dispatches instead of parking.
    await _route(tracker, issue,
                 remove=["status:drafting"], add=[TODO_LABEL, TRIAGE_MARKER])
    await wait_for(lambda: "node-1" not in orch.claimed)
    runner.release.clear()
    runner.hold = True                       # read the count before session 2
    tracker.candidates = [issue]
    await orch._tick()
    # The implement counter was cleared at the CAP-HIT RELABEL, an episode whose
    # routed state (`drafting`) the scheduler never observed — so this first tick
    # after the re-pass dispatches instead of parking.
    assert orch.sessions_for_issue("node-1") == {FAIL_REVIEW_ROLE: 1,
                                                IMPLEMENT_ROLE: 1}
    assert "node-1" not in orch.parked
    tracker.candidates = []
    runner.release.set()
    await wait_for(lambda: not orch.running)


async def test_relabel_removes_the_prior_status_label(tmp_path, monkeypatch):
    """AC: the single-status invariant holds across the cap-hit write — asserted
    on the SERVER-side label set, and with the default `expected_status` (the
    sole label is `status:todo` or `status:in-progress`, both in the default
    tuple)."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    seen = {}
    real = tracker.set_sole_status_label

    async def spy(issue_id, label, expected_status=("status:in-progress",
                                                    "status:todo")):
        seen["expected"] = expected_status
        return await real(issue_id, label, expected_status=expected_status)

    tracker.set_sole_status_label = spy
    await orch._tick()

    assert _status_labels(issue) == [FAIL_REVIEW_LABEL]
    assert (issue.id, (TODO_LABEL,)) in tracker.labels_removed
    assert seen["expected"] == ("status:in-progress", "status:todo")


async def test_set_sole_status_label_is_invoked_exactly_once(
        tmp_path, monkeypatch):
    """AC: the verifier's four routing writes are `gh issue edit` invocations
    emitted by the PROMPT MODE, not tracker calls. Across the whole fail-review
    path the tracker's guarded swap runs exactly once — the cap-hit write."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    await orch._tick()
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    await wait_for(lambda: not orch.running)
    await _route(tracker, issue, remove=[FAIL_REVIEW_LABEL], add=[TODO_LABEL])
    assert tracker.sole_status_swaps == [("node-1", FAIL_REVIEW_LABEL)]


# --- the episode bound --------------------------------------------------------


async def test_the_episode_loop_is_bounded(tmp_path, monkeypatch, capfd):
    """AC: with `gate:fail-reviewed` present, the next implement cap-hit PARKS —
    never relabels, never dispatches a verifier.

    Without this bound a retry-class verdict re-grants the full implement budget
    forever: cap-hit -> reset -> verifier -> todo -> cap-hit -> GOTO, with no
    human in the loop and `status:parked` never reached.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    # --- episode 1 --------------------------------------------------------
    await orch._tick()
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    await wait_for(lambda: not orch.running)
    verdicts_after_episode_1 = len(tracker.comments)
    await _route(tracker, issue, remove=[FAIL_REVIEW_LABEL], add=[TODO_LABEL])

    # --- episode 2: burn the re-granted implement budget --------------------
    orch.sessions_per_issue[("node-1", IMPLEMENT_ROLE)] = \
        orch._cfg.agent().max_sessions_per_issue
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    await orch._tick()

    await wait_for(lambda: "node-1" in orch.parked)
    assert PARK_LABEL in issue.labels
    assert tracker.sole_status_swaps == [("node-1", FAIL_REVIEW_LABEL)]  # once
    # At cap, and NOT reset — the park promise is that the counter stays spent.
    # The fail-review counter is episode 1's, left at 1/1 because the second
    # cap-hit never opened an episode.
    assert orch.sessions_for_issue("node-1") == {
        IMPLEMENT_ROLE: 2, FAIL_REVIEW_ROLE: 1}
    # No second verifier ran, so no second verdict was invited. (The park notice
    # is the only comment the orchestrator itself posts.)
    assert len(runner.turns) == 1
    park_comments = [c for c in tracker.comments if "parked" in c[1].lower()]
    assert len(park_comments) == 1
    assert len(tracker.comments) == verdicts_after_episode_1 + 1
    # ...and the park comment tells the operator how to RE-ARM the diagnosis:
    # unparking alone re-grants budget with no second verdict, forever.
    assert FAIL_REVIEW_MARKER in park_comments[0][1]


async def test_fail_review_cap_out_parks_and_nothing_redispatches(
        tmp_path, monkeypatch):
    """AC: the verifier capping (or dying) routes to `status:parked`, and a
    parked issue is refused at the `active_states` check with the PARK_LABEL
    gate as a second stop behind it. No recursion."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    await orch._tick()                                     # -> fail review
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()                                     # the one verifier
    await wait_for(lambda: not orch.running)
    assert orch.sessions_for_issue("node-1") == {FAIL_REVIEW_ROLE: 1}

    # The verifier died without routing: cap 1 is spent, so the next dispatch
    # parks. This is the accepted tradeoff, not an oversight.
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    await orch._tick()
    await wait_for(lambda: "node-1" in orch.parked)
    assert PARK_LABEL in issue.labels
    assert "fail review budget exhausted (1/1 fail review sessions)" \
        in tracker.comments[-1][1]
    # _park strips the fail-review claim label and backfills, so the issue reads
    # `parked` while parked and `todo` once unparked.
    assert _status_labels(issue) == [PARK_LABEL, TODO_LABEL]
    assert issue.state == "parked"

    before = len(runner.turns)
    for _ in range(3):
        await orch._tick()
    assert len(runner.turns) == before                     # nothing re-dispatched
    assert not orch._should_dispatch(issue)


async def test_a_verify_role_cap_hit_still_parks(tmp_path, monkeypatch):
    """AC: #31 changed the routing for IMPLEMENT-role cap-hits ONLY. Relabelling
    a triage cap-out would raise `handoff_preempted` — `status:triage` is in
    neither entry of `set_sole_status_label`'s default `expected_status`."""
    tmpl = FAIL_REVIEW_TMPL.replace(
        'active_states: ["todo", "in progress", "fail review"]',
        'active_states: ["triage", "todo", "in progress", "fail review"]')
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    issue = make_issue(1, "triage")
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    orch.sessions_per_issue[("node-1", VERIFY_ROLE)] = \
        orch._cfg.agent().max_sessions_per_issue

    await orch._tick()
    await wait_for(lambda: "node-1" in orch.parked)

    assert PARK_LABEL in issue.labels
    assert FAIL_REVIEW_LABEL not in issue.labels
    assert FAIL_REVIEW_MARKER not in issue.labels
    assert tracker.sole_status_swaps == []                 # no relabel attempted
    assert "verify budget exhausted" in tracker.comments[-1][1]
    # `status:triage` sorts AFTER `status:parked`, so it is KEPT: derivation
    # stays `parked` while parked, and the unpark resumes verification.
    assert _status_labels(issue) == [PARK_LABEL, "status:triage"]
    assert issue.state == "parked"
    issue.labels.remove(PARK_LABEL)
    assert normalize_status_state(issue.labels, closed=False) == "triage"


async def test_a_human_relabel_does_not_consume_the_fail_review_episode(
    tmp_path, monkeypatch
):
    """Issue #178: the marker-ABSENT case, which is this suite's stake in that
    ticket and the reason it is tested here rather than beside the other #178
    tests.

    `human-review -> todo` has two sanctioned actors and, before #178, only the
    orchestrator's own path reset the implement counter. So a reviewer's
    revision request on a ticket with a spent budget arrived at the cap branch
    above with `gate:fail-reviewed` absent — the ordinary case, since most
    tickets have never failed — and was routed to a fail-review episode. Two
    distinct harms, and the second is the worse one: the operator asked for a
    revision and got a diagnostic verifier dispatched against a failure that
    never happened, AND because the marker is once-per-issue and durable, the
    request PERMANENTLY consumed the issue's only fail-review episode on a
    non-failure. The verifier would not even address the review comments the
    human wrote.

    So the assertion that matters most here is the negative one: the marker
    must still be UNWRITTEN afterwards, meaning the episode is still available
    for the genuine cap-hit it was reserved for.

    The comment log is left write-only deliberately — one round is granted, so
    nothing reads a marker back. The read path (and with it the bound actually
    binding at two rounds) is covered in `test_review_response.py`, which
    mirrors posted comments into `fetch_pr_comments`.
    """
    tmpl = FAIL_REVIEW_TMPL.replace(
        "polling:", f'review_response:\n  bot_logins: ["{RR_BOT}"]\n\npolling:')
    _enable_app_identity(monkeypatch, RR_SELF)
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    issue = rr_issue(1)
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    _bind_pr(tracker, 1)
    assert FAIL_REVIEW_MARKER not in issue.labels          # never failed

    # Tick 1: a gate state. Observed by the relabel watcher, dispatched by
    # nothing — which is what makes the next transition attributable to a human.
    await orch._tick()
    assert runner.turns == []

    # The precondition the whole ticket is about: the budget is already spent
    # when the reviewer asks for changes. With budget left the old code
    # dispatched anyway and this test would pass against the bug.
    orch.sessions_per_issue[("node-1", IMPLEMENT_ROLE)] = \
        orch._cfg.agent().max_sessions_per_issue

    # A reviewer's `gh issue edit`: two plain label writes, no orchestrator
    # write, no `expected_status` guard.
    await tracker.add_labels("node-1", [TODO_LABEL])
    await tracker.remove_labels("node-1", [HUMAN_REVIEW_LABEL])

    runner.hold = True
    await orch._tick()

    # Asserted BEFORE awaiting the turn, and in this order, so the test fails on
    # the HARM rather than on a bookkeeping probe: with the observer removed the
    # cap branch relabels and stamps the marker synchronously inside this tick,
    # so these four lines are what go red. A test whose first failing assertion
    # is an internal attribution detail would still be red for the right reason
    # and would teach the next reader nothing about why it matters.
    assert FAIL_REVIEW_MARKER not in issue.labels    # episode still UNSPENT
    assert FAIL_REVIEW_LABEL not in issue.labels
    assert tracker.sole_status_swaps == []           # no relabel attempted
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 1}  # fresh + 1

    # ...and what dispatched is the implement session the operator asked for.
    await wait_for(lambda: len(runner.turns) == 1)
    assert _status_labels(issue) == [IN_PROGRESS_LABEL]
    assert "node-1" not in orch.parked
    assert PARK_LABEL not in issue.labels

    tracker.candidates = []                  # quiesce the continuation retry
    runner.release.set()
    await wait_for(lambda: not orch.running)


# --- park never strands -------------------------------------------------------


@pytest.mark.parametrize("state_label, expect_after_park", [
    # KEPT (sorts after status:parked; derivation stays `parked`)
    (TODO_LABEL, [PARK_LABEL, TODO_LABEL]),
    ("status:triage", [PARK_LABEL, "status:triage"]),
    # STRIPPED (orchestrator-owned claim labels, sort BEFORE status:parked) and
    # therefore backfilled to status:todo
    (IN_PROGRESS_LABEL, [PARK_LABEL, TODO_LABEL]),
    (FAIL_REVIEW_LABEL, [PARK_LABEL, TODO_LABEL]),
])
async def test_park_never_strands_an_issue_on_unpark(
        tmp_path, monkeypatch, state_label, expect_after_park):
    """AC: for every park-reachable state, assert the post-park label set, then
    remove `status:parked` and assert the derived state is in `active_states`.

    An issue stripped to `[status:parked]` ALONE derives `"none"` — in no
    `active_states` list — so `fetch_candidate_issues` and `_should_dispatch`
    both drop it the moment the operator performs the documented recovery.
    """
    tmpl = FAIL_REVIEW_TMPL.replace(
        'active_states: ["todo", "in progress", "fail review"]',
        'active_states: ["triage", "todo", "in progress", "fail review"]')
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, tmpl)
    issue = make_issue(1, "todo")
    issue.labels = [state_label, TRIAGE_MARKER]
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    await orch._park(issue, "test")

    assert _status_labels(issue) == sorted(expect_after_park)
    assert issue.state == "parked"                       # sorted-first derivation
    assert normalize_status_state(issue.labels, closed=False) == "parked"

    issue.labels.remove(PARK_LABEL)                      # the operator unparks
    unparked = normalize_status_state(issue.labels, closed=False)
    assert unparked in orch._cfg.tracker().active_states, \
        f"{state_label} stranded on unpark: derived {unparked!r}"


async def test_park_backfill_survives_a_failing_strip(tmp_path, monkeypatch):
    """AC failure case: with `remove_labels` raising, the post-park set must
    still refuse dispatch AND still unpark into an active state.

    ADD-FIRST is what makes this hold. Remove-first would leave `[status:parked]`
    alone whenever the backfill then failed — and `_park`'s existing
    `except TrackerError` swallows failures as cosmetic, which is true for a
    failed STRIP and false for a failed BACKFILL.

    ONE DIVERGENCE FROM THE AC'S LITERAL WORDING, and it is structural rather
    than a gap in the implementation. The AC asks that the post-park set "still
    derives to `parked`" here. It cannot, once a strip has failed: the labels
    `PARK_STRIP_LABELS` removes are *by construction* exactly the park-reachable
    ones that sort BEFORE `status:parked`, so any label that survives a failed
    strip is a label that wins sorted-first derivation. The ticket body concedes
    this in the same breath it calls a failed strip cosmetic — the durable
    refusal is the `PARK_LABEL` gate, which reads the LABEL, not the derived
    state. So this asserts the property the AC is actually protecting: the issue
    stays undispatchable, and the operator's unpark still lands somewhere the
    poll can see. See the PR body.
    """
    orch, tracker, runner, _ = _build_harness(
        tmp_path, monkeypatch, FAIL_REVIEW_TMPL)
    issue = make_issue(1, "todo")
    issue.labels = [FAIL_REVIEW_LABEL, TRIAGE_MARKER, FAIL_REVIEW_MARKER]
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    tracker.remove_labels_error = TrackerError("github_api_status", "boom")

    await orch._park(issue, "test")

    assert PARK_LABEL in issue.labels
    assert TODO_LABEL in issue.labels                    # backfill landed FIRST
    # The strip raised, so `status:fail-review` survives and wins derivation.
    assert normalize_status_state(issue.labels, closed=False) == FAIL_REVIEW_ROLE
    # ...and the issue is nonetheless undispatchable, which is the whole point:
    # the gate keys on the PARK_LABEL, never on the derived state.
    assert orch._should_dispatch(issue) is False

    issue.labels.remove(PARK_LABEL)                      # the operator unparks
    # What add-first actually buys: never `[status:parked]` ALONE, so the
    # documented recovery derives an active state instead of `"none"`.
    assert normalize_status_state(issue.labels, closed=False) \
        in orch._cfg.tracker().active_states


async def test_hold_route_label_set_matches_the_park_invariant(
        tmp_path, monkeypatch):
    """AC hold-route case: the `hold` verdict is written by the VERIFIER and
    bypasses `_park()` entirely, so the prompt mode's literal command has to
    satisfy the same invariant on its own. Apply it to a fake and check."""
    orch, tracker, runner, _ = _build_harness(
        tmp_path, monkeypatch, FAIL_REVIEW_TMPL)
    issue = make_issue(1, "todo")
    issue.labels = [FAIL_REVIEW_LABEL, TRIAGE_MARKER, FAIL_REVIEW_MARKER]
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}

    # The literal from WORKFLOW.base.md's hold row:
    #   --remove-label status:fail-review --add-label status:parked,status:todo
    await _route(tracker, issue,
                 remove=[FAIL_REVIEW_LABEL], add=[PARK_LABEL, TODO_LABEL])

    assert _status_labels(issue) == sorted([PARK_LABEL, TODO_LABEL])
    assert issue.state == "parked"
    issue.labels.remove(PARK_LABEL)
    assert normalize_status_state(issue.labels, closed=False) \
        in orch._cfg.tracker().active_states
    # `self.parked` is never populated for a hold-park, so the unpark-time reset
    # does not fire — harmless: the episode-start reset already cleared them.
    assert "node-1" not in orch.parked


async def test_the_verdict_comment_does_not_un_park(tmp_path, monkeypatch):
    """AC park-echo: posting the verdict does not re-arm dispatch.

    The verifier's LAST act before routing is a `## Fail-review verdict`
    comment, and on GitHub a comment bumps the issue's `updatedAt` — the signal
    the pre-#28 scheduler keyed re-dispatch off, which is how OBS-022's
    self-unpark loop worked: a capped agent commented, the bump read as
    activity, and the cap re-armed itself. #28 made the LABEL authoritative
    instead. #31 adds a session that comments on exactly these two states, so
    the property is re-asserted here rather than assumed to carry over.

    The fake models the bump faithfully (it would be fake-washing not to), so
    the assertion is against a real `updatedAt` change, not a no-op.
    """
    orch, tracker, runner, _ = _build_harness(
        tmp_path, monkeypatch, FAIL_REVIEW_TMPL)

    # (a) mid-diagnosis: the verdict comment lands while the issue is at
    #     `status:fail-review`. It must stay there — the comment is not a route.
    diagnosing = make_issue(1, "fail review", fail_reviewed=True)
    diagnosing.labels.append(TRIAGE_MARKER)
    # (b) post-hold: the same comment on an issue the `hold` route already
    #     parked. This is the one that used to bite.
    held = make_issue(2, "fail review", fail_reviewed=True)
    held.labels.append(TRIAGE_MARKER)
    tracker.candidates = [diagnosing, held]
    tracker.states = {"node-1": diagnosing, "node-2": held}

    # Park `held` through the prompt mode's literal `hold` command rather than by
    # assigning labels: the fake RECOMPUTES state from labels on a tracker write
    # (and only on a tracker write), so hand-assigning would leave a fixture
    # whose state and labels disagree — exactly the fake-fidelity failure
    # METHODOLOGY.md names.
    await _route(tracker, held,
                 remove=[FAIL_REVIEW_LABEL], add=[PARK_LABEL, TODO_LABEL])
    assert held.state == "parked"

    before = {i.id: i.updated_at for i in (diagnosing, held)}

    for issue in (diagnosing, held):
        await tracker.add_issue_comment(
            issue.id, "## Fail-review verdict\n\nblockage:permission\n")

    # The bump is real...
    for issue in (diagnosing, held):
        assert issue.updated_at > before[issue.id], \
            "the fake must model GitHub's updatedAt bump, or this proves nothing"

    # ...and authority still rests with the labels, which nobody touched.
    assert _status_labels(diagnosing) == [FAIL_REVIEW_LABEL]
    assert diagnosing.state == FAIL_REVIEW_ROLE
    assert _status_labels(held) == sorted([PARK_LABEL, TODO_LABEL])
    assert held.state == "parked"

    # The parked issue stays undispatchable. Note WHERE it is refused: `parked`
    # is in no `active_states` list, so the state check stops it first and the
    # PARK_LABEL gate never runs — which is why `orch.parked` stays empty here.
    # The label gate is the second stop, for the case where a park-reachable
    # label still derives an active state (see the failing-strip test).
    assert orch._should_dispatch(held) is False
    assert "node-2" not in orch.parked
    # The diagnosing issue is unaffected — a verdict comment is not what ends
    # its episode; the route it writes next is.
    assert orch._should_dispatch(diagnosing) is True


async def test_divergence_case_marker_failure_parks_without_stranding(
        tmp_path, monkeypatch):
    """AC divergence case: the status write SUCCEEDS, the `gate:fail-reviewed`
    write then raises, and the assertion runs against the fake's SERVER-SIDE
    label set — not `issue.labels`.

    This is the path `_route_to_fail_review`'s in-memory label reconciliation
    exists for. `set_sole_status_label` does not mutate the in-memory `Issue`,
    and the marker-failure path hands that same `Issue` to `_park()`, whose
    backfill decision reads exactly that field. Left stale it would still say
    `status:todo`, no backfill would fire, and the SERVER would end at
    `[status:parked]` alone.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    server = make_issue(1, "todo")            # a DISTINCT object: the server view
    server.labels = list(issue.labels)
    tracker.states = {"node-1": server}
    tracker.candidates = [issue]

    real_add = tracker.add_labels

    async def add_labels(issue_id, label_names):
        if FAIL_REVIEW_MARKER in label_names:
            raise TrackerError("github_api_status", "marker write boom")
        return await real_add(issue_id, label_names)

    tracker.add_labels = add_labels
    await orch._tick()

    await wait_for(lambda: "node-1" in orch.parked)
    assert _status_labels(server) == sorted([PARK_LABEL, TODO_LABEL])
    assert TRIAGE_MARKER in server.labels
    assert FAIL_REVIEW_MARKER not in server.labels
    assert normalize_status_state(server.labels, closed=False) == "parked"
    server.labels.remove(PARK_LABEL)
    assert normalize_status_state(server.labels, closed=False) \
        in orch._cfg.tracker().active_states


# --- error handling on the cap-branch writes ----------------------------------


async def test_unprovisioned_fail_review_label_falls_back_to_park(
        tmp_path, monkeypatch):
    """AC: `register-project.sh` runs at scaffold time only, so every project
    registered before this ships lacks `status:fail-review`. The fallback is
    load-bearing — without it nothing writes `status:parked`, nothing sets
    `_park_label_missing`, the issue retries the failing write every tick at
    `status:in-progress`, and a restart re-grants a full budget.

    The dispatch halt must NOT arm: `status:parked` IS provisioned on that whole
    population, so `_park`'s own writes succeed. Arming it would stop ALL
    dispatch runner-wide over one project missing one optional label.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    async def missing(issue_id, label, expected_status=None):
        raise TrackerError("github_label_not_found", "status:fail-review")

    tracker.set_sole_status_label = missing
    await orch._tick()

    await wait_for(lambda: "node-1" in orch.parked)
    assert PARK_LABEL in issue.labels
    assert FAIL_REVIEW_LABEL not in issue.labels
    assert orch._park_label_missing is None, \
        "the halt is reserved for a MISSING status:parked"
    assert _status_labels(issue) == sorted([PARK_LABEL, TODO_LABEL])
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}   # at cap


async def test_a_missing_park_label_still_arms_the_halt(tmp_path, monkeypatch):
    """The other half of the pair: the EXISTING semantics are untouched. A fake
    that rejects `status:parked` too arms the runner-wide halt, exactly as it
    did before #31 — an unwritable park label makes the cap unenforceable across
    restarts."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    async def missing(issue_id, label, expected_status=None):
        raise TrackerError("github_label_not_found", "status:fail-review")

    tracker.set_sole_status_label = missing
    tracker.add_labels_error = TrackerError("github_label_not_found",
                                            "status:parked")
    await orch._tick()

    assert orch._park_label_missing is not None
    assert "node-1" not in orch.parked


async def test_a_transient_status_write_error_touches_nothing_and_retries(
        tmp_path, monkeypatch):
    """AC: a generic `TrackerError` means NOTHING LANDED. The counter stays at
    cap, the label set is unchanged, no park comment is posted — and the next
    driven tick retries the write and succeeds.

    Parking here would be wrong (transients self-heal) and resetting here would
    be catastrophic: a full implement budget re-granted with no relabel, no
    verifier and no verdict, repeating on every 5xx.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    real = tracker.set_sole_status_label
    calls = {"n": 0}

    async def flaky(issue_id, label, expected_status=("status:in-progress",
                                                      "status:todo")):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TrackerError("github_api_status", "502")
        return await real(issue_id, label, expected_status=expected_status)

    tracker.set_sole_status_label = flaky
    await orch._tick()

    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}   # at cap
    assert _status_labels(issue) == [TODO_LABEL]                      # untouched
    assert tracker.comments == []                                     # no park
    assert "node-1" not in orch.parked
    assert "node-1" not in orch.claimed
    assert runner.turns == []

    await orch._tick()                                                # retry
    assert _status_labels(issue) == [FAIL_REVIEW_LABEL]
    assert FAIL_REVIEW_MARKER in issue.labels
    assert orch.sessions_for_issue("node-1") == {}


async def test_handoff_preempted_leaves_the_issue_untouched(
        tmp_path, monkeypatch):
    """AC: a newer transition won. Parking would clobber a deliberate human move
    — the same posture the preemption guard itself takes. Reachable without any
    concurrency: a human relabels between the candidate fetch and the write."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    async def preempted(issue_id, label, expected_status=None):
        raise TrackerError("handoff_preempted", "a human moved it")

    tracker.set_sole_status_label = preempted
    await orch._tick()

    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}   # at cap
    assert _status_labels(issue) == [TODO_LABEL]
    assert tracker.comments == []
    assert "node-1" not in orch.parked
    assert PARK_LABEL not in issue.labels


async def test_ambiguous_swap_is_repaired_by_the_idempotent_dispatch_guard(
        tmp_path, monkeypatch):
    """AC: `handoff_swap_uncertain` from the FINAL read-back — the swap applied
    server-side, clean single label, the board already shows the transition.

    A retry-the-write story is fiction for exactly this branch: the next tick
    dispatches at `fail review` and the cap branch is never re-entered. So the
    repair is the dispatch guard, which restores the marker and the counter
    reset before dispatching — keeping the episode bound EXACT, not off-by-one.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    real = tracker.set_sole_status_label

    async def uncertain(issue_id, label, expected_status=("status:in-progress",
                                                          "status:todo")):
        await real(issue_id, label, expected_status=expected_status)
        raise TrackerError("handoff_swap_uncertain", "final read-back failed")

    tracker.set_sole_status_label = uncertain
    await orch._tick()

    # The swap LANDED but the cap branch never saw it succeed: no marker, and
    # the counters were never cleared.
    assert _status_labels(issue) == [FAIL_REVIEW_LABEL]
    assert FAIL_REVIEW_MARKER not in issue.labels
    assert orch.sessions_for_issue("node-1") == {IMPLEMENT_ROLE: 2}
    assert "node-1" not in orch.parked

    # Next tick: the guard repairs both, then exactly one verifier dispatches.
    tracker.states = {"node-1": make_issue(1, "human review")}
    await orch._tick()
    assert FAIL_REVIEW_MARKER in issue.labels
    assert orch.sessions_for_issue("node-1") == {FAIL_REVIEW_ROLE: 1}
    await wait_for(lambda: not orch.running)
    assert len(runner.turns) == 1

    # ...and the bound is exact: a later retry-class re-entry that caps PARKS
    # rather than opening a second episode.
    await _route(tracker, issue, remove=[FAIL_REVIEW_LABEL], add=[TODO_LABEL])
    orch.sessions_per_issue[("node-1", IMPLEMENT_ROLE)] = \
        orch._cfg.agent().max_sessions_per_issue
    tracker.candidates = [issue]
    tracker.states = {"node-1": issue}
    await orch._tick()
    await wait_for(lambda: "node-1" in orch.parked)
    assert len(runner.turns) == 1                       # no second verifier


async def test_rollback_failure_parks_from_the_dual_status_state(
        tmp_path, monkeypatch):
    """AC: `handoff_label_rollback_failed` — the add landed, the strip failed and
    the rollback failed, leaving the dual-status state the tracker's own
    docstring says needs operator repair. `_park()` is the honest terminal: it
    strips `status:fail-review` from the dual set, leaving `status:todo`, which
    unparks cleanly."""
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)

    async def rollback_failed(issue_id, label, expected_status=None):
        # Mirror the real failure's SERVER state: the add landed, the strip did
        # not, so both status labels are present.
        await tracker.add_labels(issue_id, [FAIL_REVIEW_LABEL])
        raise TrackerError("handoff_label_rollback_failed", "dual status")

    tracker.set_sole_status_label = rollback_failed
    await orch._tick()

    await wait_for(lambda: "node-1" in orch.parked)
    assert _status_labels(issue) == sorted([PARK_LABEL, TODO_LABEL])
    assert issue.state == "parked"
    issue.labels.remove(PARK_LABEL)
    assert normalize_status_state(issue.labels, closed=False) \
        in orch._cfg.tracker().active_states


async def test_concurrent_dispatch_during_the_cap_branch_is_refused(
        tmp_path, monkeypatch):
    """AC: the cap branch holds the claim across its writes, so a second entrant
    driven through `_should_dispatch` — the sole claim reader; `_dispatch` itself
    has no claim check and is not the guarded surface — is refused.

    This is what makes "invoked exactly once on the whole fail-review path" true
    under CONCURRENCY, not just under a sequential drive.
    """
    orch, tracker, runner, issue, _ = _fail_review_harness(tmp_path, monkeypatch)
    barrier = asyncio.Event()
    entered = asyncio.Event()
    real = tracker.set_sole_status_label
    calls = {"n": 0}

    async def gated(issue_id, label, expected_status=("status:in-progress",
                                                      "status:todo")):
        calls["n"] += 1
        entered.set()
        await barrier.wait()
        return await real(issue_id, label, expected_status=expected_status)

    tracker.set_sole_status_label = gated

    task = asyncio.create_task(orch._tick())
    await asyncio.wait_for(entered.wait(), timeout=3)

    # The claim is held across the awaits; the guarded surface refuses.
    assert issue.id in orch.claimed
    assert orch._should_dispatch(issue) is False

    barrier.set()
    await asyncio.wait_for(task, timeout=3)

    assert calls["n"] == 1
    assert tracker.sole_status_swaps == [("node-1", FAIL_REVIEW_LABEL)]
    marker_writes = [w for w in tracker.labels_added
                     if w[1] == (FAIL_REVIEW_MARKER,)]
    assert len(marker_writes) == 1                 # cap branch AND dispatch guard
    assert "node-1" not in orch.parked
    assert "node-1" not in orch.claimed             # released before REFUSED


# --- the committed artifacts (asserted in CI, not by inspection) -------------


def test_both_labels_are_provisioned_and_required_by_the_shell_scripts():
    """AC: asserted by a pytest case that READS both script files, so it runs in
    CI. `register-project.sh` provisions them at scaffold time;
    `run-self-pilot-checkpoint.sh` hard-fails when either is missing."""
    register = (REPO_ROOT / "scripts" / "register-project.sh") \
        .read_text(encoding="utf-8")
    for label in (FAIL_REVIEW_LABEL, FAIL_REVIEW_MARKER):
        assert f'mklabel "{label}"' in register, \
            f"{label} is not provisioned by register-project.sh"

    checkpoint = (REPO_ROOT / "scripts" / "run-self-pilot-checkpoint.sh") \
        .read_text(encoding="utf-8")
    required = checkpoint.split("PROVISIONED_LABELS=")[1].split("done")[0]
    for label in (FAIL_REVIEW_LABEL, FAIL_REVIEW_MARKER):
        assert label in required, \
            f"{label} is not in the checkpoint's required-label loop"


def _prompt_mode(rel: str) -> str:
    """The fail-review label-conditional block, from one of the two files that
    carry it — a hand-edit to one and not the other is the drift
    `test_workflow.py` already guards for `active_states`, and this mode is now
    the largest hand-edited block in either file."""
    text = (REPO_ROOT / rel).read_text(encoding="utf-8")
    return text.split('contains "status:fail-review" %}')[1].split("{% else %}")[0]


def _flow(rel: str) -> str:
    """The mode with newlines collapsed. The prose is hard-wrapped at 80 columns,
    so a phrase assertion against the raw text would be an assertion about where
    the line breaks fall."""
    return " ".join(_prompt_mode(rel).lower().split())


# Parametrized by PATH, not by content: a content parameter makes every failure
# id a full workflow file.
PROMPT_MODE_FILES = [
    "workflow/WORKFLOW.base.md",
    "projects/switchboard-self/WORKFLOW.md",
]


@pytest.mark.parametrize("rel", PROMPT_MODE_FILES)
def test_the_prompt_mode_carries_the_posture_block(rel):
    """AC: no-code / no-commit / no-PR, enforced as PROSE — the same posture
    triage runs under. Allowlist/hook-altitude enforcement is a named follow-up,
    deliberately out of scope."""
    lowered = _flow(rel)
    assert "never write feature code" in lowered
    assert "never commit" in lowered
    assert "never open" in lowered and "pr" in lowered
    assert "do not open a pr" in lowered
    assert "handoff-evidence.json" in lowered      # explicitly NOT the verifier's


@pytest.mark.parametrize("rel", PROMPT_MODE_FILES)
def test_the_prompt_mode_carries_exactly_three_routing_commands(rel):
    """AC: three distinct `gh issue edit` strings covering the FOUR verdict
    classes — `iteration` and `complexity` share byte-identical flags."""
    mode = _prompt_mode(rel)
    edits = [l.strip() for l in mode.splitlines()
             if l.strip().startswith("gh issue edit")]
    assert len(edits) == 3, edits
    # Everything after the repo argument — the repo itself differs between the
    # base template's placeholder and the composed binding's literal.
    flags = [e.split("--repo", 1)[1].strip().split(" ", 1)[1] for e in edits]

    # retry class: same body, same diagnosis -> BOTH markers retained
    assert flags[0] == \
        "--remove-label status:fail-review --add-label status:todo"
    # re-scope: the re-drafted body earns a fresh diagnosis, and clearing the
    # triage marker is what keeps re-entry gated on a human re-draft PLUS a PASS
    assert flags[1] == (
        "--remove-label status:fail-review,gate:fail-reviewed,"
        "gate:triage-passed --add-label status:drafting")
    # hold: the status:todo backfill is what stops the unpark stranding
    assert flags[2] == (
        "--remove-label status:fail-review "
        "--add-label status:parked,status:todo")


@pytest.mark.parametrize("rel", PROMPT_MODE_FILES)
def test_the_prompt_mode_pins_the_verdict_comment_and_the_evidence_tiers(rel):
    """AC: classification + cited evidence + recommended recovery, with
    disagreement shown both ways; and self-reports read LAST, as claims
    (anti-anchoring)."""
    mode = _prompt_mode(rel)
    lowered = _flow(rel)
    assert "## fail-review verdict" in lowered          # the grep anchor
    assert "## in brief" in lowered
    assert "cited evidence" in lowered
    assert "recommended recovery" in lowered
    assert "disagree" in lowered
    # tiers, in order, with self-reports last
    assert ".run/transcripts/" in mode
    assert "self-reports last" in lowered
    assert lowered.index("mechanical digest") < lowered.index("self-reports last")
    assert "never quote transcript content" in lowered  # no transcripts on GitHub
    # every #16 class plus #31's own escape hatch
    for cls in ("blockage:permission", "blockage:dependency", "quota",
                "iteration", "complexity", "hold"):
        assert cls in mode
    # no bare failure->todo: complexity is a RECOMMENDATION, not an action
    assert "you do not file the children" in lowered


@pytest.mark.parametrize("rel", PROMPT_MODE_FILES)
def test_the_hold_route_states_the_unpark_affordance(rel):
    """A `hold` bypasses `_park()`, so no park notice is posted — the verdict
    comment has to carry the affordance itself, in the same words the park
    comment uses."""
    lowered = _flow(rel)
    assert "will not dispatch this issue while it carries" in lowered
    assert "removing that label" in lowered
    assert "workspace is preserved" in lowered


# --- transitions.yml: the table must match SHIPPED behaviour -----------------


def _edges() -> list[dict]:
    raw = yaml.safe_load(
        (REPO_ROOT / "workflow" / "transitions.yml").read_text(encoding="utf-8"))
    return raw["edges"]


def _norm(s: str) -> str:
    return str(s).replace("-", " ").strip().lower()


def _active_pairs() -> set[tuple[str, str]]:
    return {(_norm(e["from"]), _norm(e["to"])) for e in _edges()
            if e.get("active", True) is not False}


def test_every_pair_the_implementation_can_emit_has_an_active_edge():
    """AC: enumerated from the cap branch, the fallback, the park backfill, the
    unpark round trip, and the four verdict routes — so "the table matches
    shipped behaviour" is MECHANICAL rather than by inspection.

    The lookup normalizes spellings: the table is hyphenated while the
    implementation emits `normalize_status_state` output, which uses spaces.
    """
    emitted = {
        # the cap branch (todo is the DOMINANT entry — the cap check runs before
        # the in-progress claim write, and the claim reverts to todo between
        # sessions)
        ("todo", "fail review"),
        ("in progress", "fail review"),
        # the unprovisioned-label fallback + the episode cap, from either state
        ("todo", "parked"),
        ("in progress", "parked"),
        # the verify-role cap-out, which #31 leaves alone
        ("triage", "parked"),
        # the fail-review session capping out, the marker-write-failure park,
        # the rollback-failure park, and the verifier's own `hold` verdict
        ("fail review", "parked"),
        # the four verdict routes
        ("fail review", "todo"),
        ("fail review", "drafting"),
        # the unpark round trip, one per park-reachable state (the park strip
        # backfills in-progress/fail-review to todo, and keeps todo/triage)
        ("parked", "todo"),
        ("parked", "triage"),
    }
    missing = emitted - _active_pairs()
    assert not missing, f"emitted pairs with no active edge: {sorted(missing)}"


def test_no_fail_review_edge_is_still_gated_on_the_parent_ticket():
    for e in _edges():
        if "fail-review" in (e.get("from"), e.get("to")):
            assert e.get("active", True) is True, e
            assert "requires" not in e, e


def test_the_fail_review_routes_out_carry_the_corrected_actor():
    """#29 pre-encoded `actor: human` on all three routes out, which contradicts
    the ratified auto-route: they are written by the VERIFIER SESSION."""
    out = {_norm(e["to"]): e for e in _edges() if e.get("from") == "fail-review"}
    assert set(out) == {"todo", "drafting", "parked"}
    assert out["todo"]["actor"] == "fail-review-verifier"
    assert out["drafting"]["actor"] == "fail-review-verifier"
    # parked has BOTH actors, on the `human-review -> todo` precedent.
    assert set(out["parked"]["actor"]) == {"fail-review-verifier", "orchestrator"}
    assert out["parked"]["verdict"] == "hold"
    assert out["parked"]["trigger"] == "cap-hit"
    # re-scope re-enters triage, so the marker is cleared — same as the
    # `parked -> drafting` precedent this mirrors.
    assert out["drafting"]["remove_marker"] == TRIAGE_MARKER


def test_multi_producer_edges_name_every_producer_in_their_note():
    """Dual-producer discipline: where one pair has several producers, the
    annotation has to name them, or the table records a half-truth."""
    by_pair = {}
    for e in _edges():
        by_pair.setdefault((_norm(e["from"]), _norm(e["to"])), []).append(e)

    for pair, needles in {
        # verifier hold + fail-review cap-out + marker-write-failure park +
        # rollback-failure park
        ("fail review", "parked"): ["hold", "budget", "marker", "rollback"],
        # unprovisioned-label fallback + episode cap
        ("todo", "parked"): ["github_label_not_found", "episode"],
        ("in progress", "parked"): ["github_label_not_found", "episode"],
    }.items():
        notes = " ".join(e.get("note", "") for e in by_pair[pair]).lower()
        for needle in needles:
            assert needle.lower() in notes, \
                f"{pair} note does not name producer {needle!r}"
