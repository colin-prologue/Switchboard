"""Review-response triggering: the predicate and the durable round marker.

Owned Switchboard extension (issue #43 / AgDR-037). Worker PRs accumulate bot
review comments AFTER the authoring session ends, so a human-review PR reaches
Colin unreconciled with its own bot review. This module is the pure half of the
loop the scheduler's sub-poll drives: it decides whether a PR review thread is
still owed a Switchboard reply, and it owns the format of the durable per-PR
round marker that bounds how many response rounds one PR can trigger.

Pure functions over already-fetched threads/comments: no I/O, no writes. The
tracker owns the GraphQL; the scheduler owns cadence, binding, and the writes.

ONE predicate
-------------
`needs_response(thread, ...)` — the thread is UNRESOLVED **and** its last
external-bot comment postdates the last Switchboard reply in that thread (or
there is no such reply). Trigger = any bound-PR thread needs a response;
DONE = none does. One predicate, so trigger and termination cannot diverge.

- "External bot comment" means an author login in `bot_logins` (config). The
  list is the botness definition; Switchboard's own login is never in it.
- "Switchboard reply" means an author login equal to the NORMALIZED
  `$SB_APP_BOT_LOGIN`. Normalization strips a trailing `[bot]`: GraphQL returns
  the bare login (`switchboard-agent`, `__typename: Bot`) while git's author
  form is `switchboard-agent[bot]`, and the env var is operator-set free text.
  Verified live against PR #132 on 2026-08-09.
- A HUMAN reply inside a bot thread is NOT a Switchboard reply and does not
  suppress the trigger. The human suppression mechanism is RESOLVING the thread,
  which kills the first conjunct.
- The worker's own replies cannot self-retrigger (wrong login), and a style
  dismissal terminates the loop with no push at all (the reply postdates the bot
  comment).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from .types import IssueComment, ReviewThread

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)

# Durable per-PR round marker (house convention: an HTML comment whose FIRST
# line is machine-readable). It has TWO machine readers in different processes
# on different cadences — the sub-poll (count + `bots=` + `self=`) and the
# prompt addendum's guard (`bots=`, `self=`) — so this format is a two-sided
# contract and any change to it is a two-sided change.
_ROUND_MARKER_RE = re.compile(
    r"<!--\s*switchboard:response-round\s+n=(\d+)\s+bots=(\S*)\s+self=(\S+)\s*-->"
)

# The cap comment carries its OWN guard marker: at the cap the round marker is
# present by construction and therefore cannot guard the cap comment.
CAP_MARKER = "<!-- switchboard:response-cap -->"

# The human path's cap comment (issue #178) is a SECOND one-shot with its own
# marker, not a reuse of `CAP_MARKER`. The two say different things — the
# sub-poll's names unanswered bot threads and offers "relabel to todo" as the
# recovery, which is precisely the action a human at this cap has just taken —
# and a shared guard would let whichever fired first suppress the other's
# explanation on the same PR.
RELABEL_CAP_MARKER = "<!-- switchboard:relabel-cap -->"

# Rounds one PR may trigger. The DURABLE outer bound (the marker lives on the
# PR, so it survives an orchestrator restart); the freshly-reset per-role
# session cap is the inner one, giving a bounded worst case of 2 x cap sessions.
#
# ONE budget, TWO actors (issue #178): a human changes-requested relabel draws
# on this same count. That is deliberate — the cap exists so no actor can refill
# an issue's implement allowance indefinitely, and a per-actor count would let
# an operator alternate with the sub-poll to get 2x the rounds.
ROUND_CAP = 2


def normalize_login(value: str | None) -> str | None:
    """Canonical GitHub login: lower-cased, `[bot]` suffix stripped.

    `$SB_APP_BOT_LOGIN` is operator-set free text and the only existing consumer
    (`fold.py`) discards rather than matches it, so HEAD proves no convention.
    GraphQL returns the bare login for a Bot author, so both `switchboard-agent`
    and `switchboard-agent[bot]` must normalize to the same thing or the
    predicate's second conjunct silently never matches.
    """
    if not isinstance(value, str):
        return None
    login = value.strip().lower()
    if login.endswith("[bot]"):
        login = login[: -len("[bot]")]
    return login or None


def _at(value: datetime | None) -> datetime:
    """A comparable timestamp; a missing `createdAt` sorts oldest."""
    if value is None:
        return _EPOCH
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def needs_response(
    thread: ReviewThread,
    *,
    bot_logins: tuple[str, ...] | list[str],
    self_login: str | None,
) -> bool:
    """Whether `thread` is still owed a Switchboard reply (the ONE predicate).

    `bot_logins` and `self_login` are matched after normalization, so callers
    may pass raw config/env values.
    """
    if thread.is_resolved:
        return False
    me = normalize_login(self_login)
    if me is None:
        # No identity => the second conjunct is uncomputable. The feature is
        # gated off upstream; refusing here too keeps the predicate honest
        # rather than degrading to "every bot thread needs a response".
        return False
    bots = {b for b in (normalize_login(x) for x in bot_logins) if b}
    if not bots:
        return False

    last_bot: datetime | None = None
    last_self: datetime | None = None
    for comment in thread.comments:
        login = normalize_login(comment.login)
        if login is None:
            continue
        when = _at(comment.created_at)
        if login == me:
            # Self BEFORE the allowlist (codex review, PR #134): an operator
            # who accidentally lists the App login in `bot_logins` must not
            # have every Switchboard reply counted as an external bot comment
            # — that would trigger sessions off our own replies.
            if last_self is None or when > last_self:
                last_self = when
        elif login in bots:
            if last_bot is None or when > last_bot:
                last_bot = when
    if last_bot is None:
        return False
    return last_self is None or last_bot > last_self


def format_round_marker(
    n: int, bot_logins: tuple[str, ...] | list[str], self_login: str
) -> str:
    """The normative first line of a round-marker comment.

    The sub-poll always writes the CURRENTLY PARSED config into every marker it
    writes — it holds the config and never needs to read a list back. An
    operator config edit between rounds therefore leaves two lists on one PR,
    and the newest governs by the `max(n)` read rule below.
    """
    # Serialize NORMALIZED identities (codex review, PR #134): GitHub supplies
    # bare author logins, and the worker matches comments against `bots=` — a
    # `codex-bot[bot]` config form preserved here would never match and the
    # worker would find no owed thread while the rounds burn.
    bots = ",".join(b for b in (normalize_login(x) for x in bot_logins) if b)
    me = normalize_login(self_login) or ""
    return f"<!-- switchboard:response-round n={n} bots={bots} self={me} -->"


def _first_line(body: str | None) -> str:
    text = (body or "").lstrip()
    return text.splitlines()[0] if text else ""


def _authored_by(comment: IssueComment, me: str | None) -> bool:
    """Marker trust (codex review, PR #134): machine markers count only when
    authored by the normalized Switchboard identity — any other commenter
    posting a matching marker could inflate `n` to the cap (denying the owed
    response session) or fake the cap comment. Unset identity trusts nothing;
    the feature is gated off upstream in that state anyway."""
    return me is not None and normalize_login(comment.login) == me


def latest_round(
    comments: list[IssueComment], *, self_login: str | None
) -> tuple[int, str | None]:
    """`(max n, the body of the marker carrying it)` across `comments`.

    Read rule: the count is the MAX `n` across FIRST-LINE markers authored by
    the Switchboard identity — duplicates and gaps are tolerated, max wins,
    and ZERO markers means 0 (so the first write is `n=1`). The write rule is
    `max(n) + 1`. First-line matching (the PR #132 convention) keeps a comment
    QUOTING a marker from counting as one.
    """
    me = normalize_login(self_login)
    best = 0
    best_body: str | None = None
    for comment in comments:
        if not _authored_by(comment, me):
            continue
        match = _ROUND_MARKER_RE.match(_first_line(comment.body))
        if match:
            n = int(match.group(1))
            if n >= best:
                best, best_body = n, comment.body
    return best, best_body


def has_cap_comment(
    comments: list[IssueComment],
    *,
    self_login: str | None,
    marker: str = CAP_MARKER,
) -> bool:
    """Whether the one-shot cap comment guarded by `marker` was already posted.

    Same trust + first-line rule as `latest_round`. `marker` selects WHICH
    one-shot is being asked about — the sub-poll's (`CAP_MARKER`, the default,
    so existing callers are unchanged) or the human relabel path's
    (`RELABEL_CAP_MARKER`, issue #178)."""
    me = normalize_login(self_login)
    return any(
        _authored_by(c, me) and _first_line(c.body).startswith(marker)
        for c in comments
    )
