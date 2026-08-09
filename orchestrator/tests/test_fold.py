"""Fold-signal detection rules (issue #51 part a).

Pure resolution over already-fetched comments: binding, operator filtering,
and precedence. No I/O — the GraphQL shape is pinned in test_tracker.py and the
poll wiring in test_integration.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator.fold import detect_fold_signals
from orchestrator.types import CommentReaction, IssueComment

UTC = timezone.utc
T0 = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40
OPERATORS = ("colin-prologue",)


def verdict(id_: str, minutes: int, sha1: str = SHA_A, reactions=()) -> IssueComment:
    return IssueComment(
        id=id_,
        body=f"## Triage verdict\nbody-sha1: {sha1}\n\nNEEDS WORK: unstated assumption.",
        login="switchboard-agent",
        created_at=T0 + timedelta(minutes=minutes),
        reactions=tuple(reactions),
    )


def comment(id_: str, minutes: int, body: str, login: str = "colin-prologue") -> IssueComment:
    return IssueComment(
        id=id_, body=body, login=login, created_at=T0 + timedelta(minutes=minutes)
    )


def reaction(id_: str, content: str, login: str = "colin-prologue", minutes: int = 5):
    return CommentReaction(
        id=id_, content=content, login=login, created_at=T0 + timedelta(minutes=minutes)
    )


def detect(comments, operators=OPERATORS, bot_login=None):
    return detect_fold_signals(
        issue_id="node-51",
        issue_identifier="51",
        comments=comments,
        operator_logins=operators,
        bot_login=bot_login,
    )


# --- reaction channel ---------------------------------------------------------

def test_operator_thumbs_up_on_a_verdict_is_a_bound_approval():
    signals, rejected = detect([verdict("V1", 0, reactions=[reaction("RE_1", "THUMBS_UP")])])
    assert rejected == []
    assert len(signals) == 1
    sig = signals[0]
    assert (sig.channel, sig.approved, sig.verdict_comment_id) == ("reaction", True, "V1")
    # A reaction is already comment-bound: it needs no binding rule, and it
    # carries the digest of the verdict it sits on for part (b)'s CAS.
    assert sig.body_sha1 == SHA_A
    assert sig.source_node_id == "RE_1"


def test_thumbs_down_reaction_vetoes():
    signals, _ = detect([verdict("V1", 0, reactions=[reaction("RE_1", "THUMBS_DOWN")])])
    assert [(s.channel, s.approved) for s in signals] == [("reaction", False)]


def test_thumbs_down_outranks_thumbs_up_regardless_of_order():
    """A standing 👎 is an objection, not an event: it wins even when it landed
    first and even from a different operator."""
    reactions = [
        reaction("RE_1", "THUMBS_DOWN", login="colin-prologue", minutes=1),
        reaction("RE_2", "THUMBS_UP", login="second-op", minutes=9),
    ]
    signals, _ = detect([verdict("V1", 0, reactions=reactions)],
                        operators=("colin-prologue", "second-op"))
    assert [(s.approved, s.source_node_id) for s in signals] == [(False, "RE_1")]


def test_non_operator_and_non_thumb_reactions_are_ignored():
    reactions = [
        reaction("RE_1", "THUMBS_UP", login="random-passerby"),
        reaction("RE_2", "HEART"),
        CommentReaction(id="RE_3", content="THUMBS_UP", login=None),
    ]
    signals, _ = detect([verdict("V1", 0, reactions=reactions)])
    assert signals == []


def test_bot_login_is_never_an_operator_even_if_allowlisted():
    """The agent cannot approve its own verdict: the bot identity is discarded
    from the allowlist, not merely absent from it."""
    reactions = [reaction("RE_1", "THUMBS_UP", login="switchboard-agent")]
    signals, _ = detect(
        [verdict("V1", 0, reactions=reactions)],
        operators=("switchboard-agent",),
        bot_login="Switchboard-Agent",
    )
    assert signals == []


def test_reactions_on_non_verdict_comments_are_ignored():
    chatter = IssueComment(
        id="IC_1", body="looks good to me", login="colin-prologue",
        created_at=T0, reactions=(reaction("RE_1", "THUMBS_UP"),),
    )
    signals, _ = detect([chatter])
    assert signals == []


def test_no_verdict_comment_means_no_signals():
    signals, rejected = detect([comment("IC_1", 1, "/fold")])
    assert (signals, rejected) == ([], [])


def test_empty_operator_allowlist_disables_detection():
    signals, _ = detect(
        [verdict("V1", 0, reactions=[reaction("RE_1", "THUMBS_UP")])], operators=()
    )
    assert signals == []


# --- comment channel + binding rule -------------------------------------------

def test_fold_comment_binds_to_the_most_recent_preceding_verdict():
    comments = [
        verdict("V1", 0, sha1=SHA_A),
        verdict("V2", 10, sha1=SHA_B),
        comment("IC_1", 20, "/fold"),
    ]
    signals, _ = detect(comments)
    assert [(s.verdict_comment_id, s.body_sha1, s.approved) for s in signals] == [
        ("V2", SHA_B, True)
    ]


def test_fold_comment_before_any_verdict_binds_to_nothing():
    signals, rejected = detect([comment("IC_1", 0, "/fold"), verdict("V1", 10)])
    assert (signals, rejected) == ([], [])


def test_fold_comment_with_body_sha1_binds_to_that_verdict_not_the_latest():
    comments = [
        verdict("V1", 0, sha1=SHA_A),
        verdict("V2", 10, sha1=SHA_B),
        comment("IC_1", 20, f"/fold\nbody-sha1: {SHA_A}"),
    ]
    signals, _ = detect(comments)
    assert [(s.verdict_comment_id, s.body_sha1) for s in signals] == [("V1", SHA_A)]


def test_fold_comment_with_unknown_body_sha1_is_rejected_not_rebound():
    """An explicit digest that names no verdict must NOT silently fall back to
    the latest verdict — that would fold a verdict the operator did not read."""
    comments = [verdict("V1", 0, sha1=SHA_A), comment("IC_1", 20, f"/fold\nbody-sha1: {SHA_B}")]
    signals, rejected = detect(comments)
    assert signals == []
    assert [(r.comment_id, r.body_sha1) for r in rejected] == [("IC_1", SHA_B)]


def test_no_fold_comment_vetoes():
    signals, _ = detect([verdict("V1", 0), comment("IC_1", 5, "/no-fold not yet")])
    assert [(s.channel, s.approved) for s in signals] == [("comment", False)]


def test_command_must_own_its_line():
    """A verdict that *mentions* the command, or prose quoting it, is not a
    signal — only a line starting with the command counts."""
    comments = [
        verdict("V1", 0),
        comment("IC_1", 5, "I was going to reply /fold but let's wait"),
    ]
    signals, _ = detect(comments)
    assert signals == []


def test_command_from_a_non_operator_is_ignored():
    comments = [verdict("V1", 0), comment("IC_1", 5, "/fold", login="random-passerby")]
    signals, _ = detect(comments)
    assert signals == []


# --- precedence ---------------------------------------------------------------

def test_explicit_comment_beats_a_reaction_on_the_same_verdict():
    comments = [
        verdict("V1", 0, reactions=[reaction("RE_1", "THUMBS_UP", minutes=1)]),
        comment("IC_1", 5, "/no-fold"),
    ]
    signals, _ = detect(comments)
    assert [(s.channel, s.approved, s.source_node_id) for s in signals] == [
        ("comment", False, "IC_1")
    ]


def test_latest_explicit_comment_wins():
    comments = [
        verdict("V1", 0),
        comment("IC_1", 5, "/no-fold"),
        comment("IC_2", 9, "/fold"),
    ]
    signals, _ = detect(comments)
    assert [(s.approved, s.source_node_id) for s in signals] == [(True, "IC_2")]


def test_precedence_is_resolved_per_bound_verdict():
    """A veto on one verdict must not suppress an approval bound to another."""
    comments = [
        verdict("V1", 0, sha1=SHA_A, reactions=[reaction("RE_1", "THUMBS_DOWN")]),
        verdict("V2", 10, sha1=SHA_B, reactions=[reaction("RE_2", "THUMBS_UP", minutes=11)]),
    ]
    signals, _ = detect(comments)
    assert [(s.verdict_comment_id, s.approved) for s in signals] == [
        ("V1", False), ("V2", True)
    ]


def test_detection_is_idempotent():
    comments = [
        verdict("V1", 0, reactions=[reaction("RE_1", "THUMBS_UP")]),
        comment("IC_1", 5, "/fold"),
    ]
    first, _ = detect(comments)
    second, _ = detect(comments)
    assert first == second  # the caller's dedupe on source_node_id is enough
