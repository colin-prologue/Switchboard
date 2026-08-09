"""Operator fold-signal detection (issue #51 part a).

Owned Switchboard extension. A triage NEEDS-WORK / NEEDS-DECISION verdict is
folded into the issue body by hand today; this module is the DETECTION half of
the loop — it turns an operator's approval signal on a `## Triage verdict`
comment into a validated `FoldSignal` that part (b)'s apply step consumes.

Pure functions over already-fetched comments: no I/O, no writes. The tracker
owns the GraphQL (`tracker.ISSUE_COMMENTS_QUERY`); the scheduler owns the poll
cadence and per-process dedupe.

Channels and their binding
--------------------------
- **Reaction** (👍 `THUMBS_UP` / 👎 `THUMBS_DOWN`) on a verdict comment. Already
  comment-bound — the only intrinsically unambiguous channel.
- **Comment** (`/fold` // `/no-fold`). GitHub issue comments have NO reply
  threading, so binding needs an explicit rule: a command comment approves the
  most recent `## Triage verdict` comment PRECEDING it. It MAY carry a
  `body-sha1:` line to disambiguate; if that digest names no verdict on the
  issue, the signal is REJECTED (never silently re-bound to the latest verdict).

Precedence, resolved per bound verdict:
1. An explicit `/fold` // `/no-fold` comment beats any reaction on that verdict.
2. Among explicit comments, the latest (by `created_at`) wins.
3. Reactions decide only when no explicit command is bound: any operator 👎
   vetoes (a standing objection, not an event — it outranks a 👍 regardless of
   ordering); otherwise a 👍 approves.

The hash carried on a signal is `body-sha1` — the `git hash-object` digest the
triage prompt's Step 0 computes and every post-#55 verdict comment records. One
artifact, one canonical digest; part (b) compares its CAS read against this.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .types import IssueComment

# The fixed, grep-able verdict heading (methodology/METHODOLOGY.md §Triage).
VERDICT_HEADING = "## triage verdict"

# `body-sha1: <40 hex>` — the digest the verdict reviewed (issue #55).
_BODY_SHA1_RE = re.compile(r"^body-sha1:\s*([0-9a-f]{40})\s*$", re.IGNORECASE)

# A command must OWN its line (`/fold`, optionally with trailing text), so a
# verdict quoting "reply /fold to approve" is not itself a fold signal.
_FOLD_RE = re.compile(r"^/fold\b", re.IGNORECASE)
_NO_FOLD_RE = re.compile(r"^/no-fold\b", re.IGNORECASE)

THUMBS_UP = "THUMBS_UP"
THUMBS_DOWN = "THUMBS_DOWN"

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class FoldSignal:
    """One resolved operator decision about one verdict comment.

    `source_node_id` is the comment or reaction node id that decided it.
    `dedupe_key()` combines it with the RESOLVED decision and target (PR #129
    review): an operator editing `/no-fold` → `/fold`, retargeting a digest,
    or a deletion promoting an older command all change the effective decision
    on an unchanged node id — identity must track outcome, not just node.
    `approved` False is a veto (👎 / `/no-fold`), kept
    rather than dropped so the operator can see in the log that the veto was
    seen and honoured.
    """

    issue_id: str
    issue_identifier: str
    verdict_comment_id: str
    body_sha1: str | None
    channel: str            # "comment" | "reaction"
    approved: bool
    operator_login: str
    source_node_id: str
    created_at: datetime | None = None

    def dedupe_key(self) -> str:
        """Identity of the EFFECTIVE decision, not the mutable comment."""
        return (
            f"{self.source_node_id}:{int(self.approved)}:"
            f"{self.verdict_comment_id}"
        )

    def log_fields(self) -> dict[str, object]:
        return {
            "issue_id": self.issue_id,
            "issue_identifier": self.issue_identifier,
            "verdict_comment_id": self.verdict_comment_id,
            "body_sha1": self.body_sha1,
            "channel": self.channel,
            "approved": str(self.approved).lower(),
            "operator": self.operator_login,
            "source_node_id": self.source_node_id,
            "dedupe_key": self.dedupe_key(),
        }


@dataclass(frozen=True)
class RejectedSignal:
    """A command comment that named a `body-sha1:` no verdict on this issue
    carries. Surfaced (not silently dropped) so a mistyped digest is visible."""

    issue_identifier: str
    comment_id: str
    operator_login: str
    body_sha1: str
    reason: str


def is_verdict_comment(comment: IssueComment) -> bool:
    """True when the comment's FIRST line is the fixed verdict heading."""
    first = comment.body.lstrip().splitlines()[0].strip().lower() if comment.body.strip() else ""
    return first == VERDICT_HEADING


def parse_body_sha1(body: str) -> str | None:
    """The `body-sha1:` digest a comment carries, if any (lower-cased)."""
    for line in body.splitlines():
        match = _BODY_SHA1_RE.match(line.strip())
        if match:
            return match.group(1).lower()
    return None


def parse_fold_command(body: str) -> bool | None:
    """True for `/fold`, False for `/no-fold`, None when neither is present.

    `/no-fold` is checked first: `_FOLD_RE` is anchored at line start so it
    cannot match `/no-fold`, but the explicit ordering keeps that non-accidental.
    """
    for line in body.splitlines():
        stripped = line.strip()
        if _NO_FOLD_RE.match(stripped):
            return False
        if _FOLD_RE.match(stripped):
            return True
    return None


def detect_fold_signals(
    *,
    issue_id: str,
    issue_identifier: str,
    comments: list[IssueComment],
    operator_logins: tuple[str, ...] | list[str],
    bot_login: str | None = None,
) -> tuple[list[FoldSignal], list[RejectedSignal]]:
    """Resolve the fold signals an issue's comment thread currently carries.

    Returns `(signals, rejected)` — at most one signal per bound verdict comment,
    ordered by that verdict's position in the thread. Deterministic and
    idempotent: the same thread always yields the same signals, so the caller's
    per-process dedupe on `source_node_id` is the only state needed.
    """
    operators = {
        login.strip().lower()
        for login in operator_logins
        if isinstance(login, str) and login.strip()
    }
    # The bot is never an operator: agent posts are excluded by login as well as
    # by the on-behalf-of signature convention (issue #51).
    if bot_login:
        operators.discard(bot_login.strip().lower())
    if not operators:
        return [], []

    ordered = sorted(comments, key=lambda c: (c.created_at or _EPOCH,))
    verdicts = [c for c in ordered if is_verdict_comment(c)]
    if not verdicts:
        return [], []
    by_sha1: dict[str, IssueComment] = {}
    for verdict in verdicts:
        digest = parse_body_sha1(verdict.body)
        if digest is not None:
            # LATEST matching verdict wins (PR #129 review): the fast-path can
            # legitimately stamp the same body-sha1 on several verdicts, and a
            # /fold naming that digest means the most recent one the operator
            # saw — never a stale earlier copy. `verdicts` is oldest-first, so
            # plain assignment keeps the last.
            by_sha1[digest] = verdict

    rejected: list[RejectedSignal] = []
    # verdict comment id -> winning candidate, per the precedence rules.
    commands: dict[str, tuple[datetime, FoldSignal]] = {}
    reactions: dict[str, list[FoldSignal]] = {}

    for comment in ordered:
        # --- explicit command channel ---------------------------------------
        approved = parse_fold_command(comment.body)
        if (
            approved is not None
            and comment.login in operators
            and not is_verdict_comment(comment)
        ):
            digest = parse_body_sha1(comment.body)
            if digest is not None:
                target = by_sha1.get(digest)
                if target is None:
                    rejected.append(
                        RejectedSignal(
                            issue_identifier=issue_identifier,
                            comment_id=comment.id,
                            operator_login=comment.login or "",
                            body_sha1=digest,
                            reason="body-sha1 names no verdict comment on this issue",
                        )
                    )
                    target = None
            else:
                target = _preceding_verdict(verdicts, comment)
            if target is not None:
                signal = FoldSignal(
                    issue_id=issue_id,
                    issue_identifier=issue_identifier,
                    verdict_comment_id=target.id,
                    body_sha1=parse_body_sha1(target.body),
                    channel="comment",
                    approved=approved,
                    operator_login=comment.login or "",
                    source_node_id=comment.id,
                    created_at=comment.created_at,
                )
                stamp = comment.created_at or _EPOCH
                prior = commands.get(target.id)
                if prior is None or stamp >= prior[0]:
                    commands[target.id] = (stamp, signal)

        # --- reaction channel (verdict comments only) ------------------------
        if not is_verdict_comment(comment):
            continue
        digest = parse_body_sha1(comment.body)
        for reaction in comment.reactions:
            if reaction.login not in operators:
                continue
            if reaction.content not in (THUMBS_UP, THUMBS_DOWN):
                continue
            reactions.setdefault(comment.id, []).append(
                FoldSignal(
                    issue_id=issue_id,
                    issue_identifier=issue_identifier,
                    verdict_comment_id=comment.id,
                    body_sha1=digest,
                    channel="reaction",
                    approved=reaction.content == THUMBS_UP,
                    operator_login=reaction.login or "",
                    source_node_id=reaction.id,
                    created_at=reaction.created_at,
                )
            )

    signals: list[FoldSignal] = []
    for verdict in verdicts:
        winner = commands.get(verdict.id)
        if winner is not None:
            signals.append(winner[1])
            continue
        candidates = reactions.get(verdict.id) or []
        if not candidates:
            continue
        veto = next((s for s in candidates if not s.approved), None)
        signals.append(veto if veto is not None else candidates[0])
    return signals, rejected


def _preceding_verdict(
    verdicts: list[IssueComment], comment: IssueComment
) -> IssueComment | None:
    """The most recent verdict comment preceding `comment` (the binding rule)."""
    stamp = comment.created_at or _EPOCH
    prior = [v for v in verdicts if (v.created_at or _EPOCH) <= stamp and v.id != comment.id]
    return prior[-1] if prior else None
