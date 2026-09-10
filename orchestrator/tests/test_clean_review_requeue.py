"""The return path from the human gate (issue #198).

The escalation this reverses is the QA role's fail-closed act: a review bot that
has not reviewed the current head sha means "do not SHIP", so the ticket goes to
the human gate. It had no way back. A CLEAN review opens no threads, so
`needs_response` — the only thing that looked at review state afterwards — saw
nothing, and the ticket sat at a gate waiting for the relabel automation had
promised to make unnecessary.

Its own file rather than more of `test_review_response.py`: that suite is about
one predicate over threads, and this is a disjoint second question over reviews,
reactions, and marker comments. It reuses that suite's harness helpers rather
than building a second set.

FIXTURE FIDELITY (OBS-023): a review fixture stores the commit oid it read and
its state enum; a reaction fixture stores its content enum, reacting login, and
`createdAt`. Neither ever stores "this one is clean" — `clean_review_signal`
derives that on every assertion below, exactly as it does from the server's
payload. A fixture holding the answer would pass its own tests and lie about the
system.

What these tests are careful about:

- **Both completion shapes.** A bot's clean verdict is EITHER a formal review
  with no findings OR a bare 👍. Testing one and shipping both re-creates the
  indefinite wait one layer down, which is the defect itself.
- **The sha pin is asserted per shape, because the shapes pin differently.** A
  formal review carries the commit it read; a reaction carries no commit at all
  and is pinned indirectly, through the marker. Covering only the review shape
  would leave the reaction shape free to requeue on a superseded diff.
- **Every refusal is asserted against the LABEL, not merely a return value.**
  The refusals exist to keep a ticket at the gate, and that is a property of the
  board.
"""

from __future__ import annotations

from orchestrator.review_response import (
    CLEAN_REVIEW_STATES,
    clean_review_signal,
    format_requeue_marker,
    format_round_marker,
    latest_escalation,
    needs_response,
    requeue_recorded,
    sha_matches,
    unresolved_bot_threads,
)
from orchestrator.scheduler import HUMAN_REVIEW_LABEL, TODO_LABEL
from orchestrator.types import IssueComment

from test_integration import (  # the shared fakes; this suite adds no second set
    RR_BOT,
    RR_HEAD_SHA,
    RR_OLD_SHA,
    RR_QA_TMPL,
    RR_SELF,
    RR_T0,
    RR_TMPL,
    _requeue_harness,
    rr_escalation,
    rr_reaction,
    rr_review,
    rr_thread,
)
from test_review_response import (
    HUMAN,
    _publish_pr_comments,
    _relabel,
    _round_markers,
)

REVIEW_LABEL = "status:review"


# --- sha matching: the pin AC 3 rests on --------------------------------------

def test_a_full_sha_matches_itself_and_nothing_else():
    assert sha_matches("a" * 40, "a" * 40) is True
    assert sha_matches("a" * 40, "b" * 40) is False


def test_an_abbreviated_sha_matches_the_oid_it_prefixes():
    """The marker is WORKER-authored free text and the prompt permits an
    abbreviation, so git's own >=7-hex rule applies. A session that wrote
    `sha=abc1234` must not silently lose its return path."""
    assert sha_matches("a" * 7, "a" * 40) is True
    assert sha_matches("a" * 40, "a" * 7) is True


def test_an_abbreviation_shorter_than_git_allows_is_refused():
    """Six hex digits collide across a real repo's history. Accepting them would
    let a requeue fire on a commit that merely shares a prefix with the head."""
    assert sha_matches("a" * 6, "a" * 40) is False


def test_a_missing_sha_never_matches():
    """`head_sha` comes from `pr.get(...)` and the marker's sha is worker text;
    neither may be treated as a wildcard."""
    assert sha_matches(None, "a" * 40) is False
    assert sha_matches("a" * 40, None) is False
    assert sha_matches("", "") is False


# --- reading the escalation marker --------------------------------------------

def test_an_escalation_marker_is_read_back_off_its_own_formatter():
    esc = latest_escalation([rr_escalation()], self_login=RR_SELF)
    assert esc is not None
    assert (esc.sha, esc.bot) == (RR_HEAD_SHA, RR_BOT)


def test_a_forged_escalation_marker_is_not_trusted():
    """Same trust rule as the round marker (PR #134). Without it ANY commenter
    could name a bot of their choosing and drive the requeue off that bot's
    approval — an attacker-chosen cross-model check."""
    assert latest_escalation([rr_escalation(login="mallory")],
                             self_login=RR_SELF) is None


def test_a_comment_quoting_an_escalation_marker_does_not_count_as_one():
    """First-line matching, the house convention — a session explaining the
    mechanism in prose must not arm it."""
    quoting = rr_escalation(prefix="The marker we write reads:\n\n")
    assert latest_escalation([quoting], self_login=RR_SELF) is None


def test_the_last_escalation_wins_not_the_first():
    """These markers carry no ordinal (unlike the round marker's `n=`), and a PR
    accumulates one per escalation round. The newest names the sha actually
    being waited on; an older one names a commit the head has moved past."""
    old = rr_escalation(sha=RR_OLD_SHA, minutes=0, id_="IC_old")
    new = rr_escalation(sha=RR_HEAD_SHA, minutes=30, id_="IC_new")
    esc = latest_escalation([old, new], self_login=RR_SELF)
    assert esc is not None and esc.sha == RR_HEAD_SHA


def test_a_marker_naming_no_bot_is_not_an_escalation():
    """`bot=` is the whole question the clean check answers. A marker without a
    usable login asks nothing, so it must not arm a requeue against nobody."""
    broken = IssueComment(
        id="IC_b", login=RR_SELF,
        body=f"<!-- switchboard:escalated-pending-review sha={RR_HEAD_SHA} bot= -->",
    )
    assert latest_escalation([broken], self_login=RR_SELF) is None


def test_the_requeue_receipt_is_read_back_per_sha():
    """The one-shot bound is keyed on the sha, not the PR: a genuinely new
    commit deserves its own return path."""
    receipt = IssueComment(
        id="IC_r", login=RR_SELF, body=format_requeue_marker(RR_HEAD_SHA))
    assert requeue_recorded([receipt], self_login=RR_SELF, sha=RR_HEAD_SHA) is True
    assert requeue_recorded([receipt], self_login=RR_SELF, sha=RR_OLD_SHA) is False
    forged = IssueComment(id="IC_f", login="mallory",
                          body=format_requeue_marker(RR_HEAD_SHA))
    assert requeue_recorded([forged], self_login=RR_SELF, sha=RR_HEAD_SHA) is False


# --- what counts as "nothing left open" ---------------------------------------

def test_an_unresolved_thread_blocks_even_when_no_reply_is_owed():
    """STRICTER than `not needs_response(...)`, on purpose. A thread Switchboard
    has already answered owes nothing, but the stance prompt is explicit that an
    unresolved thread means the work is not finished — so it is not clean."""
    answered = rr_thread((RR_BOT, 0), (RR_SELF, 5))
    assert needs_response(
        answered, bot_logins=(RR_BOT,), self_login=RR_SELF) is False
    assert unresolved_bot_threads([answered], bot_login=RR_BOT) == [answered]


def test_a_resolved_thread_is_not_open():
    assert unresolved_bot_threads(
        [rr_thread((RR_BOT, 0), resolved=True)], bot_login=RR_BOT) == []


def test_a_thread_the_bot_never_touched_is_not_its_thread():
    """Someone else's open thread is not this bot's pending finding, and the
    escalation was about this bot."""
    assert unresolved_bot_threads([rr_thread((HUMAN, 0))], bot_login=RR_BOT) == []


# --- the two clean-completion shapes ------------------------------------------

def signal(*, reviews=(), reactions=(), since=RR_T0, bot=RR_BOT,
           head=RR_HEAD_SHA):
    return clean_review_signal(
        reviews=list(reviews), reactions=list(reactions), bot_login=bot,
        head_sha=head, since=since)


def test_a_formal_review_of_the_head_sha_is_a_clean_signal():
    assert signal(reviews=[rr_review(state="APPROVED")]) == "review"


def test_a_commented_review_counts_as_clean_too():
    """Requiring APPROVED would re-create the indefinite wait for every bot that
    posts a summary review without formally approving — precisely the failure
    mode that keying on the wrong channel produces."""
    assert "COMMENTED" in CLEAN_REVIEW_STATES
    assert signal(reviews=[rr_review(state="COMMENTED")]) == "review"


def test_changes_requested_is_never_clean_even_with_no_threads():
    """It says on its face that the work is not done."""
    assert signal(reviews=[rr_review(state="CHANGES_REQUESTED")]) is None


def test_a_clean_review_of_a_superseded_commit_is_not_a_signal():
    """AC 3, at the predicate. The head moved after the bot read it, so its
    verdict describes a diff that no longer exists."""
    assert signal(reviews=[rr_review(state="APPROVED", oid=RR_OLD_SHA)]) is None


def test_a_review_by_someone_else_is_not_the_bots_review():
    assert signal(reviews=[rr_review(login=HUMAN, state="APPROVED")]) is None


def test_a_bare_thumbs_up_is_the_other_clean_shape():
    """The codex reality: a clean approval may be ONLY a +1. No formal review
    exists to read, so the reaction is the entire signal."""
    assert signal(reactions=[rr_reaction()]) == "reaction"


def test_a_thumbs_up_predating_the_escalation_is_not_about_this_wait():
    """A reaction carries no commit, so `since` is half of its sha pin: a 👍 the
    bot left BEFORE the QA session escalated cannot be the review it waited
    for."""
    assert signal(reactions=[rr_reaction(minutes=-10)]) is None


def test_without_an_escalation_time_the_reaction_shape_is_refused():
    """That pin is entirely indirect — marker sha still current AND reaction
    postdating the marker. With no marker timestamp the second half is
    unavailable, so the shape is refused rather than guessed at."""
    assert signal(reactions=[rr_reaction()], since=None) is None


def test_a_thumbs_down_after_the_escalation_vetoes_both_shapes():
    """Whatever a 👎 means it is not "this is clean" — and it must override the
    formal review too, since a bot that approves and then thumbs-down is not a
    bot whose approval should move a ticket."""
    assert signal(reviews=[rr_review(state="APPROVED")],
                  reactions=[rr_reaction(content="THUMBS_DOWN")]) is None


def test_a_thumbs_up_from_someone_else_is_not_the_bots_signal():
    """An operator's encouraging 👍 is not a cross-model review."""
    assert signal(reactions=[rr_reaction(login=HUMAN)]) is None


def test_no_signal_at_all_is_the_ordinary_still_pending_case():
    """The escalation was correct and stays in force — what the poll sees on
    almost every tick."""
    assert signal() is None


# --- the wiring: does the board actually move? --------------------------------

def _receipts(tracker) -> list[str]:
    return [body for _id, body in tracker.comments
            if body.startswith("<!-- switchboard:requeued-clean-review")]


async def test_a_clean_formal_review_returns_the_ticket_to_qa(
    tmp_path, monkeypatch, capfd
):
    """AC 1, shape one — the ticket's whole point. The bot posted its review
    after the escalation, it was clean, so the reason for the escalation is gone
    and the ticket returns to the QA state it came from."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]

    assert await orch._poll_review_responses(tracker) == ["198"]

    assert tracker.sole_status_swaps == [(issue.id, REVIEW_LABEL)]
    assert issue.labels.count(REVIEW_LABEL) == 1
    assert HUMAN_REVIEW_LABEL not in issue.labels
    # The receipt is written BEFORE the relabel, the ordering both sibling paths
    # use: a crash between them costs one requeue opportunity, while the reverse
    # leaves a requeue the one-shot bound can never account for.
    assert tracker.comments[0][0] == f"PR_node_{pr}"
    assert _receipts(tracker) == [tracker.comments[0][1]]
    assert tracker.comments[0][1].startswith(format_requeue_marker(RR_HEAD_SHA))
    assert "signal: `review`" in tracker.comments[0][1]
    assert "review requeue" in capfd.readouterr().err


async def test_a_bare_thumbs_up_returns_the_ticket_to_qa(tmp_path, monkeypatch):
    """AC 1, shape two — the one the ticket flagged as the way to get this
    wrong. With NO formal review on the PR at all, the 👍 is the entire clean
    signal; keying only on `reviews` would park this ticket forever."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_reactions[pr] = [rr_reaction()]

    assert await orch._poll_review_responses(tracker) == ["198"]
    assert tracker.sole_status_swaps == [(issue.id, REVIEW_LABEL)]
    assert "signal: `reaction`" in tracker.comments[0][1]


async def test_a_clean_review_of_a_superseded_commit_leaves_the_ticket_parked(
    tmp_path, monkeypatch
):
    """AC 3, at the board. The worker pushed again after escalating, so the
    bot's clean verdict is about a diff that is no longer under review.
    Requeueing here would SHIP an unreviewed commit on the next QA pass."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_comments[pr] = [rr_escalation(sha=RR_OLD_SHA)]
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED", oid=RR_OLD_SHA)]

    assert await orch._poll_review_responses(tracker) == []
    assert tracker.sole_status_swaps == []
    assert HUMAN_REVIEW_LABEL in issue.labels
    assert _receipts(tracker) == []


async def test_a_stale_reaction_cannot_ride_a_stale_marker_back_in(
    tmp_path, monkeypatch
):
    """The reaction shape inherits its sha pin from the marker check, so this is
    the assertion that the inheritance holds: a 👍 postdating a marker for a
    SUPERSEDED commit still does not requeue."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_comments[pr] = [rr_escalation(sha=RR_OLD_SHA)]
    tracker.pr_reactions[pr] = [rr_reaction(minutes=30)]

    assert await orch._poll_review_responses(tracker) == []
    assert HUMAN_REVIEW_LABEL in issue.labels


async def test_a_human_origin_human_review_is_never_touched(tmp_path, monkeypatch):
    """AC 2. An operator relabel, an escalation-list judgment, and a finding
    surviving two rounds all land on the SAME label. Nothing else on the board
    records why, so with no marker the requeue fails closed — to exactly today's
    behaviour, which the operator inbox digest already surfaces."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_comments[pr] = []                     # no escalation marker
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]

    assert await orch._poll_review_responses(tracker) == []
    assert tracker.sole_status_swaps == []
    assert HUMAN_REVIEW_LABEL in issue.labels


async def test_a_gated_stance_park_is_never_requeued_and_costs_nothing(
    tmp_path, monkeypatch
):
    """AC 5. At a stance whose Gate C is a HUMAN's, `status:review` is not a
    state anything dispatches — requeueing into it would strand the ticket
    somewhere strictly worse than the gate it already sits at. Checked before
    any read, so a gated project (every `base` project, and switchboard-self
    itself) spends zero extra API calls on this path forever."""
    orch, tracker, _, issue, pr = _requeue_harness(
        tmp_path, monkeypatch, tmpl=RR_TMPL)
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]
    assert orch._cfg.tracker().agent_owns_gate_c() is False

    before = tracker.api_calls
    assert await orch._poll_review_responses(tracker) == []
    assert HUMAN_REVIEW_LABEL in issue.labels
    # The sub-poll's own reads (issue fetch + PR bind + threads) and NOTHING
    # beyond: no PR comments, no reviews, no reactions.
    assert tracker.api_calls - before == 3


async def test_an_unresolved_bot_thread_is_not_a_clean_review(
    tmp_path, monkeypatch
):
    """A thread Switchboard already answered owes no reply, so the findings half
    of the sub-poll passes over it — but it is still OPEN, and open means the
    work is not finished. Resolving is the human's act."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0), (RR_SELF, 5))]
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]

    assert await orch._poll_review_responses(tracker) == []
    assert HUMAN_REVIEW_LABEL in issue.labels


async def test_a_review_with_findings_still_takes_the_findings_path(
    tmp_path, monkeypatch
):
    """AC 4. With a reply owed, the ORIGINAL behaviour wins outright: the ticket
    goes to `status:todo` for a response session, not to the QA state. The
    requeue lives in the `if not owed` branch and may not reach past it."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_review_threads[pr] = [rr_thread((RR_BOT, 0))]
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]

    assert await orch._poll_review_responses(tracker) == ["198"]
    assert tracker.sole_status_swaps == [(issue.id, TODO_LABEL)]
    assert _receipts(tracker) == []
    assert tracker.comments[0][1] == format_round_marker(1, (RR_BOT,), RR_SELF)


async def test_an_escalation_naming_an_unconfigured_bot_is_refused(
    tmp_path, monkeypatch
):
    """The marker is worker-authored, so its `bot=` is not authority. A login
    outside `review_response.bot_logins` is not the project's cross-model check,
    and its approval must not move the board."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_comments[pr] = [rr_escalation(bot="some-other-bot")]
    tracker.pr_reviews[pr] = [rr_review(login="some-other-bot", state="APPROVED")]

    assert await orch._poll_review_responses(tracker) == []
    assert HUMAN_REVIEW_LABEL in issue.labels


async def test_the_requeue_is_one_shot_per_head_sha(tmp_path, monkeypatch):
    """The loop bound. A QA session that runs, escalates again at the SAME sha
    (because it still will not ship), and finds the same clean review waiting
    would otherwise be requeued onto it forever — label ping-pong burning a QA
    session every round."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]
    _publish_pr_comments(tracker, pr)

    assert await orch._poll_review_responses(tracker) == ["198"]
    assert len(_receipts(tracker)) == 1

    # The QA session escalates again on the same commit and the poll comes
    # round: the receipt on the PR is what stops a second requeue.
    await _relabel(tracker, issue, add=HUMAN_REVIEW_LABEL, remove=REVIEW_LABEL)
    tracker.pr_comments[pr].append(rr_escalation(minutes=60, id_="IC_esc2"))
    orch._review_last_poll_at = None

    assert await orch._poll_review_responses(tracker) == []
    assert len(_receipts(tracker)) == 1
    assert HUMAN_REVIEW_LABEL in issue.labels


async def test_a_new_commit_gets_its_own_return_path(tmp_path, monkeypatch):
    """The other side of the one-shot: the bound is per SHA, not per PR. A
    worker that pushed a fix and escalated again on the NEW commit is waiting on
    a genuinely different review, and must not inherit the old receipt."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_comments[pr] = [
        rr_escalation(sha=RR_HEAD_SHA, minutes=60, id_="IC_esc2"),
        IssueComment(id="IC_old_receipt", login=RR_SELF,
                     body=format_requeue_marker(RR_OLD_SHA), created_at=RR_T0),
    ]
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]

    assert await orch._poll_review_responses(tracker) == ["198"]
    assert tracker.sole_status_swaps == [(issue.id, REVIEW_LABEL)]


async def test_the_requeue_attributes_its_own_relabel(tmp_path, monkeypatch):
    """AC 7. `_observe_human_relabels` detects a human's edge as "a state change
    I did not record myself", so an unattributed orchestrator write reads as a
    human's and mints a session budget nobody asked for.

    Live rather than theoretical: `agent_owns_gate_c` is satisfied by ANY
    handoff target inside `active_states`, and `status:todo` is such a target —
    at that config the requeue writes the very label the observer watches for.
    """
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]
    orch._last_status_state[issue.id] = "human review"

    assert await orch._poll_review_responses(tracker) == ["198"]
    assert orch._last_status_state[issue.id] == "review"

    # The observer now sees a state it already recorded, so it grants nothing
    # and spends none of the two-round budget.
    assert await orch._observe_human_relabels(tracker, [issue]) == []
    assert _round_markers(tracker) == []


async def test_the_return_path_is_off_without_the_app_identity(
    tmp_path, monkeypatch
):
    """`latest_escalation` trusts only markers authored by the normalized
    `$SB_APP_BOT_LOGIN`. Unset, no marker is ever trusted — the whole sub-poll
    is already disabled upstream, and this asserts the return path did not open
    a second door around that gate."""
    orch, tracker, _, issue, pr = _requeue_harness(tmp_path, monkeypatch)
    tracker.pr_reviews[pr] = [rr_review(state="APPROVED")]
    monkeypatch.delenv("SB_APP_BOT_LOGIN", raising=False)

    assert await orch._poll_review_responses(tracker) == []
    assert HUMAN_REVIEW_LABEL in issue.labels


async def test_the_qa_stance_really_does_hand_gate_c_to_an_agent(
    tmp_path, monkeypatch
):
    """Guards the harness itself. Every requeue test above is meaningless if
    `RR_QA_TMPL` silently stopped being an agent-owned-Gate-C stance — they
    would all pass through the gated-stance refusal and assert nothing. This is
    the same posture `test_board_sanity.py` takes toward the two real stances.
    """
    orch, _, _, _, _ = _requeue_harness(tmp_path, monkeypatch, tmpl=RR_QA_TMPL)
    cfg = orch._cfg.tracker()
    assert cfg.agent_owns_gate_c() is True
    assert cfg.handoff_label == REVIEW_LABEL
    assert "review" in cfg.active_states
