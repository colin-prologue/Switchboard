"""The review-response predicate and round marker (issue #43 / AgDR-037).

Pure logic over already-fetched threads/comments. No I/O — the GraphQL shape is
pinned in test_tracker.py and the poll wiring in test_integration.py.

FIXTURE FIDELITY (OBS-023): these fixtures carry exactly what the server
derives and the predicate reads — `isResolved`, per-comment author login, and
comment `createdAt` — and never a hard-coded verdict. `needs_response` is
computed from that state on every assertion below; a fixture that stored the
answer would pass its own tests and lie about the system. Note what is
DELIBERATELY ABSENT: push times (the predicate reads none) and reactions
(Codex's 👍 is an optimization signal the loop neither hangs on nor requires).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator.review_response import (
    CAP_MARKER,
    RELABEL_CAP_MARKER,
    ROUND_CAP,
    format_round_marker,
    has_cap_comment,
    latest_round,
    needs_response,
    normalize_login,
)
from orchestrator.scheduler import (
    HUMAN_REVIEW_LABEL,
    IMPLEMENT_ROLE,
    PARK_LABEL,
    TODO_LABEL,
)
from orchestrator.types import IssueComment, ReviewThread, ReviewThreadComment

from test_integration import (  # the shared fakes; this suite adds no second set
    RR_BOT,
    RR_SELF,
    RR_TMPL,
    _bind_pr,
    _build_harness,
    _enable_app_identity,
    rr_issue,
    rr_thread,
    wait_for,
)

UTC = timezone.utc
T0 = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)

BOTS = ("chatgpt-codex-connector",)
SELF = "switchboard-agent"
HUMAN = "colin-prologue"


def c(login: str | None, minutes: int, id_: str = "RC") -> ReviewThreadComment:
    return ReviewThreadComment(
        id=f"{id_}_{minutes}", login=login, created_at=T0 + timedelta(minutes=minutes)
    )


def thread(*comments: ReviewThreadComment, resolved: bool = False) -> ReviewThread:
    return ReviewThread(id="RT_1", is_resolved=resolved, comments=comments)


def ask(t: ReviewThread, *, bots=BOTS, self_login: str | None = SELF) -> bool:
    return needs_response(t, bot_logins=bots, self_login=self_login)


# --- the ONE predicate: conjunct 1, resolution --------------------------------

def test_unanswered_bot_comment_needs_a_response():
    assert ask(thread(c(BOTS[0], 0))) is True


def test_resolved_thread_never_needs_a_response():
    """Resolving is the HUMAN suppression mechanism for a thread Switchboard
    left unresolved by design (a style dismissal, or an escalation). It kills
    the first conjunct regardless of who said what last."""
    assert ask(thread(c(BOTS[0], 0), resolved=True)) is False


# --- conjunct 2: whose comment is last ----------------------------------------

def test_switchboard_reply_after_the_bot_settles_the_thread():
    """The termination case, and the one that makes a style dismissal
    terminal: the reply postdates the bot comment even with no push at all."""
    assert ask(thread(c(BOTS[0], 0), c(SELF, 5))) is False


def test_bot_reply_after_switchboard_reopens_the_thread():
    """ACCEPTED NOISE: a chatty bot acknowledging a dismissal re-satisfies the
    predicate. Bounded by the round cap, and accepted — the PR #132 precedent
    ran three codex passes."""
    assert ask(thread(c(BOTS[0], 0), c(SELF, 5), c(BOTS[0], 10))) is True


def test_switchboards_own_reply_alone_cannot_self_retrigger():
    """No bot comment at all => nothing is owed. The worker's own posts carry
    the wrong login to be a trigger, so the loop cannot feed itself."""
    assert ask(thread(c(SELF, 0), c(SELF, 5))) is False


def test_human_reply_does_not_suppress_the_trigger():
    """A human reply inside a bot thread is NOT a Switchboard reply. The stated
    human suppression mechanism is RESOLVING the thread, not talking in it —
    otherwise a passing human remark would silently cancel the response."""
    assert ask(thread(c(BOTS[0], 0), c(HUMAN, 5))) is True


def test_only_the_last_bot_comment_matters_not_the_first():
    """Answered round 1, bot re-opened, answered again: settled."""
    t = thread(c(BOTS[0], 0), c(SELF, 5), c(BOTS[0], 10), c(SELF, 15))
    assert ask(t) is False


def test_comment_order_in_the_payload_does_not_change_the_verdict():
    """The predicate takes maxima, not payload positions — GraphQL ordering is
    not something the trigger may depend on."""
    forward = thread(c(BOTS[0], 0), c(SELF, 5))
    shuffled = thread(c(SELF, 5), c(BOTS[0], 0))
    assert ask(forward) == ask(shuffled) is False


# --- botness is exactly the allowlist -----------------------------------------

def test_a_login_outside_the_allowlist_is_not_a_bot():
    """The allowlist IS the botness definition. An unlisted reviewer — human or
    machine — never triggers a round."""
    assert ask(thread(c("some-other-bot", 0))) is False


def test_switchboard_is_never_its_own_trigger_even_if_misconfigured():
    """Codex review (PR #134): self is classified BEFORE the allowlist, so an
    operator who accidentally lists the App's own login cannot make Switchboard
    trigger response sessions off its own replies."""
    t = thread(c(SELF, 0))
    assert needs_response(t, bot_logins=(SELF,), self_login=SELF) is False
    # And a real bot comment newer than the self reply still triggers.
    t2 = thread(c(SELF, 0), c(BOTS[0], 1))
    assert needs_response(t2, bot_logins=(SELF,) + BOTS, self_login=SELF) is True


def test_empty_allowlist_disables_the_predicate():
    assert ask(thread(c(BOTS[0], 0)), bots=()) is False


def test_missing_self_identity_disables_the_predicate():
    """Without the App identity the second conjunct is uncomputable. Refusing
    is the honest answer; degrading to "every bot thread is owed" would make
    every already-answered thread trigger forever."""
    assert ask(thread(c(BOTS[0], 0)), self_login=None) is False
    assert ask(thread(c(BOTS[0], 0)), self_login="  ") is False


def test_deleted_account_comments_are_ignored():
    """A null author matches neither conjunct — the safe direction for both."""
    assert ask(thread(c(None, 0))) is False
    assert ask(thread(c(BOTS[0], 0), c(None, 5))) is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("switchboard-agent[bot]", "switchboard-agent"),
        ("Switchboard-Agent", "switchboard-agent"),
        ("  switchboard-agent  ", "switchboard-agent"),
        ("", None),
        (None, None),
    ],
)
def test_login_normalization(raw, expected):
    """`$SB_APP_BOT_LOGIN` is operator-set free text while GraphQL returns the
    BARE login for a Bot author (verified live against PR #132). Both forms must
    land on one value or the second conjunct silently never matches."""
    assert normalize_login(raw) == expected


def test_the_env_form_and_the_graphql_form_agree():
    """The end-to-end statement of the above: an operator who set the `[bot]`
    suffix still matches the bare login GraphQL reports."""
    t = thread(c(BOTS[0], 0), c("switchboard-agent", 5))
    assert needs_response(t, bot_logins=BOTS, self_login="Switchboard-Agent[bot]") is False


def test_allowlist_entries_are_normalized_too():
    t = thread(c("chatgpt-codex-connector", 0))
    assert needs_response(
        t, bot_logins=("ChatGPT-Codex-Connector[bot]",), self_login=SELF
    ) is True


def test_missing_created_at_sorts_oldest_and_never_invents_a_trigger():
    """A null `createdAt` must not read as "newest" — that would trigger a
    round off a timestamp GitHub did not give us."""
    t = ReviewThread(
        id="RT",
        is_resolved=False,
        comments=(
            ReviewThreadComment(id="a", login=BOTS[0], created_at=None),
            c(SELF, 5),
        ),
    )
    assert ask(t) is False


# --- the durable round marker -------------------------------------------------

def marker_comment(n: int, bots=BOTS, self_login=SELF, id_="IC") -> IssueComment:
    return IssueComment(
        id=f"{id_}_{n}",
        body=format_round_marker(n, bots, self_login) + "\n\nRound noted.",
        login=SELF,
        created_at=T0,
    )


def test_marker_first_line_carries_n_bots_and_self():
    """The normative format. It has TWO machine readers in different processes
    — the sub-poll (count + lists) and the prompt addendum's guard — so a
    short-form marker is a two-sided break, not a cosmetic one."""
    line = format_round_marker(1, ("codex-bot", "other-bot"), SELF)
    assert line == (
        "<!-- switchboard:response-round n=1 bots=codex-bot,other-bot "
        "self=switchboard-agent -->"
    )


def test_zero_markers_reads_as_zero_so_the_first_write_is_round_one():
    assert latest_round([], self_login=SELF) == (0, None)
    plain = IssueComment(id="x", login=HUMAN, body="just a review comment")
    assert latest_round([plain], self_login=SELF) == (0, None)


def test_the_count_is_the_max_n_not_the_marker_count():
    """Duplicates and gaps are tolerated: max wins. A retried write that posted
    the same marker twice must not consume two rounds."""
    n, _ = latest_round([marker_comment(1), marker_comment(1, id_="dup")], self_login=SELF)
    assert n == 1
    assert latest_round([marker_comment(1), marker_comment(3)], self_login=SELF)[0] == 3


def test_the_newest_marker_governs_after_a_config_edit():
    """The sub-poll writes the CURRENTLY parsed config into every marker, so an
    operator edit between rounds leaves two lists on one PR. The max(n) read
    rule decides which one a session obeys."""
    old = marker_comment(1, bots=("old-bot",))
    new = marker_comment(2, bots=("new-bot",))
    n, body = latest_round([old, new], self_login=SELF)
    assert n == 2
    assert "bots=new-bot" in body


def test_marker_is_found_regardless_of_position_in_the_comment_list():
    n, body = latest_round([
        IssueComment(id="chatter", login=HUMAN, body="unrelated"),
        marker_comment(2),
    ], self_login=SELF)
    assert (n, "self=switchboard-agent" in body) == (2, True)


def test_short_form_marker_is_not_recognized():
    """The addendum's guard reads `bots=`/`self=` off the marker; a marker
    missing them would pass a presence-only check and strand the session with
    half a predicate. The reader must reject it outright."""
    stale = IssueComment(
        id="old", login=SELF, body="<!-- switchboard:response-round n=9 -->",
    )
    assert latest_round([stale], self_login=SELF) == (0, None)


# --- the cap ------------------------------------------------------------------

def test_cap_comment_has_its_own_guard_marker():
    """At the cap the ROUND marker is present by construction, so it cannot
    guard the cap comment. Idempotence needs a separate marker."""
    assert CAP_MARKER not in format_round_marker(ROUND_CAP, BOTS, SELF)
    assert has_cap_comment([], self_login=SELF) is False
    assert has_cap_comment([marker_comment(ROUND_CAP)], self_login=SELF) is False
    capped = IssueComment(id="c", login=SELF, body=CAP_MARKER + "\ncapped")
    assert has_cap_comment([capped], self_login=SELF) is True


# --- marker trust (codex review, PR #134) -------------------------------------

def test_markers_from_other_authors_are_not_trusted():
    """A third party posting a matching marker must not inflate the count to
    the cap (denying the owed response session) or fake the cap comment."""
    forged = IssueComment(
        id="f", login="mallory",
        body=format_round_marker(ROUND_CAP, BOTS, SELF) + "\n\nforged",
    )
    assert latest_round([forged], self_login=SELF) == (0, None)
    fake_cap = IssueComment(id="fc", login="mallory", body=CAP_MARKER + "\nfake")
    assert has_cap_comment([fake_cap], self_login=SELF) is False


def test_a_comment_quoting_a_marker_does_not_count_as_one():
    """First-line matching, the PR #132 convention."""
    quoting = IssueComment(
        id="q", login=SELF,
        body="For the record the marker reads:\n\n"
             + format_round_marker(2, BOTS, SELF),
    )
    assert latest_round([quoting], self_login=SELF) == (0, None)


def test_marker_serializes_normalized_logins():
    """Config in the common `[bot]`-suffixed form must not survive into the
    marker — the worker matches bare GitHub logins against `bots=`."""
    line = format_round_marker(1, ("Codex-Bot[bot]",), "Switchboard-Agent[bot]")
    assert line == (
        "<!-- switchboard:response-round n=1 bots=codex-bot "
        "self=switchboard-agent -->"
    )


def test_the_two_cap_comments_have_separate_one_shot_guards():
    """Issue #178: the human path's cap comment is a SECOND one-shot.

    The two say different things — the sub-poll's offers "move the issue back
    to `status:todo`" as the recovery, which is the action a human at this cap
    has just taken. A shared guard would let whichever fired first suppress the
    other's explanation, so the operator would be told nothing at all."""
    assert RELABEL_CAP_MARKER != CAP_MARKER
    bot_cap = IssueComment(id="b", login=SELF, body=CAP_MARKER + "\nbot")
    human_cap = IssueComment(id="h", login=SELF, body=RELABEL_CAP_MARKER + "\nhuman")
    assert has_cap_comment([bot_cap], self_login=SELF) is True
    assert has_cap_comment(
        [bot_cap], self_login=SELF, marker=RELABEL_CAP_MARKER) is False
    assert has_cap_comment(
        [human_cap], self_login=SELF, marker=RELABEL_CAP_MARKER) is True
    assert has_cap_comment([human_cap], self_login=SELF) is False
    # Same trust rule (PR #134): a third party cannot fake either one.
    forged = IssueComment(id="f", login="mallory",
                          body=RELABEL_CAP_MARKER + "\nforged")
    assert has_cap_comment(
        [forged], self_login=SELF, marker=RELABEL_CAP_MARKER) is False


# =============================================================================
# The human changes-requested relabel (issue #178 / AgDR-049)
# =============================================================================
#
# Scheduler-level, unlike everything above, and deliberately so: the defect is
# not in the predicate but in WHICH ACTOR's write reset the session counters,
# and only the tick loop holds that. `transitions.yml` sanctions two actors on
# `human-review -> todo`; before #178 only the orchestrator's own path called
# `_reset_issue_sessions`, so a reviewer's revision request on a ticket with a
# spent implement budget hit the dispatch cap and forked on the durable
# `gate:fail-reviewed` marker — opening a fail-review episode when it was
# absent, parking when it was present. Neither is a re-dispatch, which is what
# the operator asked for.
#
# What these tests are careful about:
#
# - **The counter must already be spent.** A fresh-counter test passes while the
#   fix can never fire: with budget left the old code dispatched anyway.
# - **The marker must be READABLE on the next round.** The fake's comment log is
#   write-only, so a test without `_publish_pr_comments` would read n=0 forever
#   and the cap would appear to bind while no bound existed.
# - **Every refusal is asserted on the COUNTER, not just the return value.** The
#   grant's whole effect is a mutation nine other readers observe.

PR_NUMBER = 500
PR_NODE = f"PR_node_{PR_NUMBER}"


def _publish_pr_comments(tracker, pr_number: int = PR_NUMBER) -> None:
    """Mirror posted PR comments back into the read path (fake fidelity).

    GitHub publishes a posted comment to the conversation; the fake's
    `comments` log is write-only while `latest_round` reads `fetch_pr_comments`.
    Without the mirror every round would re-read zero markers, the cap could
    never bind, and the bound this ticket rests on would be untested.
    """
    real_add = tracker.add_issue_comment

    async def add_and_publish(issue_id, body):
        await real_add(issue_id, body)
        if issue_id == f"PR_node_{pr_number}":
            tracker.pr_comments.setdefault(pr_number, []).append(
                IssueComment(id=f"IC_{len(tracker.comments)}", body=body,
                             login=RR_SELF, created_at=T0))

    tracker.add_issue_comment = add_and_publish


def _relabel_harness(tmp_path, monkeypatch, *, extra_labels=()):
    """A `status:human-review` issue with a bound PR and the App identity set."""
    _enable_app_identity(monkeypatch, RR_SELF)
    orch, tracker, runner, _ = _build_harness(tmp_path, monkeypatch, RR_TMPL)
    issue = rr_issue(178)
    issue.labels.extend(extra_labels)
    tracker.candidates = [issue]
    tracker.states = {issue.id: issue}
    _bind_pr(tracker, 178, PR_NUMBER)
    _publish_pr_comments(tracker)
    return orch, tracker, runner, issue


def _spend_the_implement_budget(orch, issue) -> None:
    """The precondition the whole ticket is about — see the section note."""
    orch.sessions_per_issue[(issue.id, IMPLEMENT_ROLE)] = \
        orch._cfg.agent().max_sessions_per_issue


async def _relabel(tracker, issue, *, add: str, remove: str) -> None:
    """Exactly what a reviewer's `gh issue edit` does: two label writes, no
    orchestrator write and no `expected_status` guard."""
    await tracker.add_labels(issue.id, [add])
    await tracker.remove_labels(issue.id, [remove])


async def _human_requests_changes(orch, tracker, issue) -> list[str]:
    """One full round trip: park at the gate, be observed there, spend the
    budget, then have a HUMAN take the edge. Returns what the observer granted."""
    await _relabel(tracker, issue, add=HUMAN_REVIEW_LABEL, remove=TODO_LABEL)
    await orch._observe_human_relabels(tracker, [issue])
    _spend_the_implement_budget(orch, issue)
    await _relabel(tracker, issue, add=TODO_LABEL, remove=HUMAN_REVIEW_LABEL)
    return await orch._observe_human_relabels(tracker, [issue])


def _round_markers(tracker) -> list[str]:
    return [body for _id, body in tracker.comments
            if body.startswith("<!-- switchboard:response-round")]


# --- the grant ----------------------------------------------------------------

async def test_a_human_relabel_dispatches_instead_of_parking(
    tmp_path, monkeypatch
):
    """AC 2: with the durable episode marker PRESENT and the implement budget
    spent, a human relabel re-dispatches — it does not park.

    Driven through `_tick` rather than the observer alone, because the ordering
    claim is the load-bearing one: the grant must land BEFORE this same tick's
    cap check, or the operator's revision request still parks once.
    """
    orch, tracker, runner, issue = _relabel_harness(
        tmp_path, monkeypatch, extra_labels=["gate:fail-reviewed"])

    await orch._tick()                      # a gate state: observed, not dispatched
    assert runner.turns == []
    assert orch._last_status_state[issue.id] == "human review"

    _spend_the_implement_budget(orch, issue)
    await _relabel(tracker, issue, add=TODO_LABEL, remove=HUMAN_REVIEW_LABEL)

    await orch._tick()
    await wait_for(lambda: runner.turns)
    await wait_for(lambda: not orch.running)

    assert PARK_LABEL not in issue.labels
    assert issue.id not in orch.parked
    assert len(runner.turns) == 1
    # The grant is recorded durably on the PR before the counters move.
    assert _round_markers(tracker) == [
        format_round_marker(1, (RR_BOT,), RR_SELF)]


async def test_the_bound_binds_at_two_rounds_per_pr(tmp_path, monkeypatch):
    """AC 3: two granted rounds per PR, then the orchestrator comments instead.

    This is the evasion bound. Resetting a budget in response to a label a human
    typed means an operator could otherwise refill an issue's allowance by
    relabelling it, which is the cap's whole purpose defeated.
    """
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)

    for expected_round in (1, 2):
        assert await _human_requests_changes(orch, tracker, issue) == ["178"]
        assert orch.sessions_for_issue(issue.id) == {}
        assert _round_markers(tracker)[-1] == \
            format_round_marker(expected_round, (RR_BOT,), RR_SELF)

    # Round three: same actions, no grant — and the counter is left exactly as
    # it was, so the ordinary cap machinery decides what happens next.
    assert await _human_requests_changes(orch, tracker, issue) == []
    assert orch.sessions_for_issue(issue.id) == {IMPLEMENT_ROLE: 2}
    assert len(_round_markers(tracker)) == ROUND_CAP
    assert RELABEL_CAP_MARKER in tracker.comments[-1][1]
    assert PARK_LABEL in tracker.comments[-1][1]   # the recovery it names

    # ...and the cap comment is a one-shot: a fourth relabel adds no noise.
    before = len(tracker.comments)
    assert await _human_requests_changes(orch, tracker, issue) == []
    assert len(tracker.comments) == before


async def test_the_sub_polls_own_relabel_is_not_read_as_a_human_relabel(
    tmp_path, monkeypatch
):
    """AC 4: the orchestrator path is unchanged, and specifically it does not
    pay twice.

    The observer's whole detection rule is "a state change into `todo` I did not
    record myself". If the sub-poll's own relabel were not attributed, the very
    next tick would read it as a human's and burn a second round granting a
    budget that was just granted — halving the cap for the actor that had it.
    """
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)
    tracker.pr_review_threads[PR_NUMBER] = [rr_thread((RR_BOT, 0))]
    _spend_the_implement_budget(orch, issue)

    await orch._observe_human_relabels(tracker, [issue])     # seen at the gate
    assert await orch._poll_review_responses(tracker) == ["178"]
    assert orch.sessions_for_issue(issue.id) == {}
    assert len(_round_markers(tracker)) == 1

    # The issue now reads `todo`, and the observer must stay quiet about it.
    assert await orch._observe_human_relabels(tracker, [issue]) == []
    assert len(_round_markers(tracker)) == 1


# --- the refusals -------------------------------------------------------------

async def test_a_relabel_with_budget_left_costs_a_round_and_an_api_call_of_nothing(
    tmp_path, monkeypatch
):
    """The common case — an ordinary revision request on a ticket that never hit
    its cap. Nothing is spent, so a grant would buy nothing and consume one of
    two scarce rounds. It must also cost zero API calls: this path runs on every
    tick, and a PR bind + comment fetch per relabel would be pure waste."""
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)
    await orch._observe_human_relabels(tracker, [issue])
    await _relabel(tracker, issue, add=TODO_LABEL, remove=HUMAN_REVIEW_LABEL)

    before = tracker.api_calls
    assert await orch._observe_human_relabels(tracker, [issue]) == []
    assert tracker.api_calls == before
    assert _round_markers(tracker) == []


async def test_no_bindable_pr_means_no_grant(tmp_path, monkeypatch):
    """The bound lives in a comment ON THE PR. With no PR there is nowhere to
    record that a round was spent, and an unrecorded grant is exactly the
    refill-by-relabelling the cap exists to prevent — so the refusal is the
    conservative direction, not a gap."""
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)
    tracker.open_prs.clear()

    assert await _human_requests_changes(orch, tracker, issue) == []
    assert orch.sessions_for_issue(issue.id) == {IMPLEMENT_ROLE: 2}
    assert _round_markers(tracker) == []


async def test_without_the_app_identity_no_budget_is_granted(
    tmp_path, monkeypatch, capfd
):
    """`latest_round` trusts only markers authored by the normalized
    `$SB_APP_BOT_LOGIN`. Unset, the count reads 0 forever — so a grant would be
    UNBOUNDED, which is worse than the bug being fixed. Same posture the
    sub-poll takes on the same env var, and loud once."""
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)
    monkeypatch.delenv("SB_APP_BOT_LOGIN", raising=False)

    assert await _human_requests_changes(orch, tracker, issue) == []
    assert orch.sessions_for_issue(issue.id) == {IMPLEMENT_ROLE: 2}
    assert "SB_APP_BOT_LOGIN is unset" in capfd.readouterr().err


async def test_a_grant_fires_once_per_transition_not_once_per_tick(
    tmp_path, monkeypatch
):
    """The observer records the new state before granting, so an issue sitting
    at `todo` across many ticks cannot re-grant. Without this the two-round cap
    would be consumed within seconds of a single relabel."""
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)

    assert await _human_requests_changes(orch, tracker, issue) == ["178"]
    for _ in range(3):
        assert await orch._observe_human_relabels(tracker, [issue]) == []
    assert len(_round_markers(tracker)) == 1


async def test_a_restart_forgets_the_transition_and_that_is_harmless(
    tmp_path, monkeypatch
):
    """The detection map is process-lifetime, which looks like the restart
    weakness #15 describes and is not one: it has exactly the lifetime of
    `sessions_per_issue`, the state it exists to correct. The issue a fresh
    process has forgotten is the issue whose budget is already fresh — so it
    dispatches, which is the outcome the grant exists to produce."""
    orch, tracker, _, issue = _relabel_harness(tmp_path, monkeypatch)
    await orch._observe_human_relabels(tracker, [issue])
    _spend_the_implement_budget(orch, issue)
    await _relabel(tracker, issue, add=TODO_LABEL, remove=HUMAN_REVIEW_LABEL)

    restarted, _, _, _ = _build_harness(tmp_path, monkeypatch, RR_TMPL)
    assert restarted._last_status_state == {}
    assert restarted.sessions_per_issue == {}
    assert await restarted._observe_human_relabels(tracker, [issue]) == []
    assert restarted._should_dispatch(issue) is True
    assert _round_markers(tracker) == []            # and no round was spent
