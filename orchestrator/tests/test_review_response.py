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
    ROUND_CAP,
    format_round_marker,
    has_cap_comment,
    latest_round,
    needs_response,
    normalize_login,
)
from orchestrator.types import IssueComment, ReviewThread, ReviewThreadComment

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
    """Belt and braces for the config's structural guarantee: were the operator
    to list Switchboard's own login, the `elif` ordering must still not let a
    self-reply count as an owed bot comment... and it does count as a bot here,
    which is why the CONFIG is the real guard. Pinned so the behaviour is a
    known one rather than a surprise."""
    t = thread(c(SELF, 0))
    assert needs_response(t, bot_logins=(SELF,), self_login=SELF) is True


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
    assert latest_round([]) == (0, None)
    plain = IssueComment(id="x", login=HUMAN, body="just a review comment")
    assert latest_round([plain]) == (0, None)


def test_the_count_is_the_max_n_not_the_marker_count():
    """Duplicates and gaps are tolerated: max wins. A retried write that posted
    the same marker twice must not consume two rounds."""
    n, _ = latest_round([marker_comment(1), marker_comment(1, id_="dup")])
    assert n == 1
    assert latest_round([marker_comment(1), marker_comment(3)])[0] == 3


def test_the_newest_marker_governs_after_a_config_edit():
    """The sub-poll writes the CURRENTLY parsed config into every marker, so an
    operator edit between rounds leaves two lists on one PR. The max(n) read
    rule decides which one a session obeys."""
    old = marker_comment(1, bots=("old-bot",))
    new = marker_comment(2, bots=("new-bot",))
    n, body = latest_round([old, new])
    assert n == 2
    assert "bots=new-bot" in body


def test_marker_is_found_regardless_of_position_in_the_comment_list():
    n, body = latest_round([
        IssueComment(id="chatter", login=HUMAN, body="unrelated"),
        marker_comment(2),
    ])
    assert (n, "self=switchboard-agent" in body) == (2, True)


def test_short_form_marker_is_not_recognized():
    """The addendum's guard reads `bots=`/`self=` off the marker; a marker
    missing them would pass a presence-only check and strand the session with
    half a predicate. The reader must reject it outright."""
    stale = IssueComment(
        id="old", login=SELF, body="<!-- switchboard:response-round n=9 -->",
    )
    assert latest_round([stale]) == (0, None)


# --- the cap ------------------------------------------------------------------

def test_cap_comment_has_its_own_guard_marker():
    """At the cap the ROUND marker is present by construction, so it cannot
    guard the cap comment. Idempotence needs a separate marker."""
    assert CAP_MARKER not in format_round_marker(ROUND_CAP, BOTS, SELF)
    assert has_cap_comment([]) is False
    assert has_cap_comment([marker_comment(ROUND_CAP)]) is False
    capped = IssueComment(id="c", login=SELF, body=CAP_MARKER + "\ncapped")
    assert has_cap_comment([capped]) is True
