"""Operator fold-signal APPLY step (issue #126, part b of the fold loop).

Part (a) (`fold.py`) turns an operator's 👍 / `/fold` into a validated
`FoldSignal`. This module consumes an APPROVED signal and performs the fold:
rewrite the issue body from the verdict's proposal block, post the durable
fold-applied marker comment, then relabel `drafting -> triage`.

Write order — body, marker, relabel (Mechanics 7)
-------------------------------------------------
Every failable step completes while the issue is still inside the fold poll
window (`scheduler.FOLD_POLL_STATES`). The relabel is the terminal act
precisely because it evicts the issue from the poll set, so a re-emitted signal
can resume the earlier steps but never the relabel.

The loop is READ-COMPARE-WRITE, not atomic: GitHub has no conditional update,
so the TOCTOU window between the before-digest read and the body write is an
accepted residual (single-operator repo). Verify-after-write cannot catch a
clobber — it compares the STORED bytes against the INTENDED bytes, which is why
the payload semantics (whole-body replacement, Mechanics 3) are pinned here and
tested directly rather than left to a downstream check.

Idempotency is the fold-applied MARKER, not the digest. A completed fold's
after-digest survives an untouched re-triage round (fold -> re-triage NEEDS
WORK -> relabel back to drafting leaves the digest at after-sha1), so digests
alone cannot distinguish "resume a partial fold" from "done weeks ago". The
marker check therefore runs FIRST — after binding, before any digest
comparison — and it is keyed on the BOUND (proposal-bearing) comment id so two
signals reaching one proposal by different routes carry the SAME key.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .fold import FoldSignal, is_verdict_comment, parse_body_sha1
from .log import log
from .types import Issue, IssueComment, TrackerError

# --- pinned literals ---------------------------------------------------------

# Proposal sentinels (house convention per `graph_review.py:42-45`). HTML
# comments are immune to the ``` fences issue bodies routinely contain, and the
# triage prompt cites this exact pair.
PROPOSAL_OPEN = "<!-- fold:proposal -->"
PROPOSAL_CLOSE = "<!-- /fold:proposal -->"

# Non-greedy DOTALL: the match TERMINATES at the first close literal, which is
# exactly why a block-content check can never fire and the collision rule below
# has to be COUNT-based (Mechanics 3, triage round 12).
_PROPOSAL_RE = re.compile(
    re.escape(PROPOSAL_OPEN) + r"\n(?P<block>.*?)\n" + re.escape(PROPOSAL_CLOSE),
    re.DOTALL,
)

# Machine-readable first line of the fold-applied marker comment (house
# convention per `graph_review.py:40`'s LEDGER_MARKER). Keyed on the BOUND
# comment id so the key is a function of the FOLD, not of the approval route.
# The trailing space in the prefix is SIGNIFICANT: without it `verdict:IC_abc`
# would prefix-match `verdict:IC_abcdef`.
FOLD_MARKER_PREFIX = "<!-- switchboard:fold-applied verdict:"

TRIAGE_LABEL = "status:triage"
DRAFTING_LABEL = "status:drafting"

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# --- byte convention (Mechanics 5) -------------------------------------------

def blob_sha1(data: bytes) -> str:
    """`git hash-object` over `data`, computed IN-PROCESS.

    git's blob hash is `sha1(b"blob " + len + b"\\0" + data)` — NOT a plain
    sha1 of the bytes. Computed here rather than shelled out because the daemon
    is async and the hermetic-fixture discipline forbids subprocesses at test
    time.
    """
    return hashlib.sha1(
        b"blob " + str(len(data)).encode() + b"\0" + data
    ).hexdigest()


def body_digest(body: str | None) -> str:
    """The digest every verdict's `body-sha1:` line carries.

    Measured convention: `gh issue view N --json body -q .body` emits
    `body + "\\n"` unconditionally (jq -r appends a trailing newline; no CR
    bytes), and the prompt pipes exactly those bytes into `git hash-object`.
    `Issue.description` is `str | None`, so a null body hashes as `"\\n"`.
    """
    return blob_sha1(((body or "") + "\n").encode("utf-8"))


# --- proposal extraction (Mechanics 3) ---------------------------------------

def has_proposal_block(body: str) -> bool:
    """True when a comment carries the open sentinel at all (binding filter)."""
    return PROPOSAL_OPEN in body


def extract_proposal(body: str) -> tuple[str | None, str | None]:
    """`(payload, error)` for one verdict comment's proposal block.

    The payload is the COMPLETE replacement issue body — not a section
    fragment, not a patch. Exactly one newline after the open sentinel and one
    before the close sentinel are framing and excluded from the payload (the
    house discipline, `graph_review.py:45`).

    Collision rule (count-based — the only checkable formulation): the comment
    must contain EXACTLY ONE open literal and EXACTLY ONE close literal. Any
    other count is malformed. A naive non-greedy match would instead silently
    apply a payload truncated at the first close literal, green on every
    downstream check.
    """
    opens = body.count(PROPOSAL_OPEN)
    closes = body.count(PROPOSAL_CLOSE)
    if opens == 0 and closes == 0:
        return None, "no proposal block"
    if opens != 1 or closes != 1:
        return None, (
            f"malformed proposal: {opens} open / {closes} close sentinel(s), "
            f"expected exactly one of each"
        )
    match = _PROPOSAL_RE.search(body)
    if match is None:
        return None, (
            "malformed proposal: sentinels are not a newline-framed open/close "
            "pair in order"
        )
    payload = match.group("block")
    if not payload.strip():
        return None, "malformed proposal: empty payload (would blank the body)"
    return payload, None


# --- fold-applied marker (Mechanics 8) ---------------------------------------

def marker_first_line(bound_comment_id: str, before: str, after: str) -> str:
    """The single-line machine-readable marker. Emitted as ONE line — a wrapped
    emission would defeat the first-line match idempotency turns on."""
    return (
        f"{FOLD_MARKER_PREFIX}{bound_comment_id} "
        f"before:{before} after:{after} -->"
    )


def marker_comment_body(
    *, bound_comment_id: str, before: str, after: str, issue_url: str | None
) -> str:
    """The full marker comment: pinned first line, then human prose.

    Shaped so part (a)'s scanner can never re-trigger on it — it does not begin
    with `## Triage verdict` (`fold.py:117-120` keys on the first line) and
    carries no line-initial `/fold` (`fold.py:50`).
    """
    where = issue_url or "(issue url unavailable)"
    return "\n".join(
        [
            marker_first_line(bound_comment_id, before, after),
            "",
            "Switchboard folded the operator-approved triage proposal into the "
            "issue body.",
            "",
            f"- verdict: {where} (comment `{bound_comment_id}`)",
            f"- before-sha1: `{before}`",
            f"- after-sha1: `{after}`",
        ]
    )


def is_fold_marker(
    comment: IssueComment, bound_comment_id: str, *, bot_login: str | None = None
) -> bool:
    """Marker-first match: AT THE START OF THE COMMENT'S FIRST LINE.

    A PREFIX match, never equality (the sha1s vary per fold) and never a
    substring scan of the body — a comment QUOTING a marker (verdicts embed
    whole revised bodies, and bodies quote sentinels) must not spoof one. The
    empty-body guard mirrors `fold.py:118`'s own idiom.

    When `bot_login` is configured (App identity), only that author's markers
    count: any other participant posting the deterministic prefix would
    otherwise suppress a legitimate approved fold as `already_folded`.
    Unconfigured (GITHUB_TOKEN mode), any author is accepted — the
    single-operator premise.
    """
    if not comment.body.strip():
        return False
    if bot_login and (comment.login or "").strip().lower() != bot_login.strip().lower():
        return False
    first = comment.body.lstrip().splitlines()[0]
    return first.startswith(f"{FOLD_MARKER_PREFIX}{bound_comment_id} ")


def find_fold_marker(
    comments: list[IssueComment], bound_comment_id: str, *, bot_login: str | None = None
) -> IssueComment | None:
    for comment in comments:
        if is_fold_marker(comment, bound_comment_id, bot_login=bot_login):
            return comment
    return None


# --- outcome -----------------------------------------------------------------

@dataclass(frozen=True)
class FoldOutcome:
    """What apply decided about one signal.

    `consume` drives the scheduler's `fold_signals_seen` write: every DECIDED
    outcome consumes, and only a transient tracker failure on a read, the body
    write, or the marker post leaves the signal re-emittable (Mechanics 9).
    """

    status: str
    consume: bool
    detail: str
    bound_comment_id: str | None = None
    before_sha1: str | None = None
    after_sha1: str | None = None

    def log_fields(self) -> dict[str, object]:
        return {
            "status": self.status,
            "consumed": str(self.consume).lower(),
            "detail": self.detail,
            "bound_comment_id": self.bound_comment_id,
            "before_sha1": self.before_sha1,
            "after_sha1": self.after_sha1,
        }


def _decided(status: str, detail: str, **kw) -> FoldOutcome:
    return FoldOutcome(status=status, consume=True, detail=detail, **kw)


def _retry(status: str, detail: str, **kw) -> FoldOutcome:
    return FoldOutcome(status=status, consume=False, detail=detail, **kw)


# --- apply -------------------------------------------------------------------

async def apply_fold_signal(
    tracker, signal: FoldSignal, *, issue: Issue, bot_login: str | None = None
) -> FoldOutcome:
    """Apply one fold signal. Returns the outcome; never raises TrackerError.

    `issue` is the poll's already-fetched view (state + url only) — every
    decision that gates a WRITE is made against a fresh read, not this snapshot.
    """
    ident = signal.issue_identifier

    # --- free pre-checks: no API call, no write --------------------------------
    if not signal.approved:
        return _decided("vetoed", "operator vetoed this verdict; nothing folded",
                        bound_comment_id=signal.verdict_comment_id)

    if issue.state == "decision":
        # Mechanics 11: `decision -> triage` is deliberately illegal and a
        # NEEDS-DECISION verdict predates the operator's answer, so it carries
        # no proposal. Folding the chosen option stays manual.
        return _decided(
            "skipped_decision_state",
            "issue is at status:decision; folding the operator's answer is "
            "manual via decision -> drafting",
            bound_comment_id=signal.verdict_comment_id,
        )

    if signal.body_sha1 is None:
        # Retrofit: a pre-#55 verdict carries no `body-sha1:` anywhere, so
        # there is no before-digest to compare against.
        return _decided(
            "skipped_retrofit",
            "bound verdict carries no body-sha1: line (pre-#55 retrofit)",
            bound_comment_id=signal.verdict_comment_id,
        )

    # --- read 1: the thread re-read (Mechanics 2) ------------------------------
    # INSIDE apply, once per signal — never one pre-batch snapshot looped over
    # the batch, so a signal sees the marker and body writes performed by
    # earlier signals in the same poll batch.
    try:
        comments = await tracker.fetch_issue_comments(ident)
    except TrackerError as exc:
        return _retry("read_failed", f"thread re-read failed: {exc}",
                      bound_comment_id=signal.verdict_comment_id)

    by_id = {c.id: c for c in comments}
    verdict = by_id.get(signal.verdict_comment_id)
    if verdict is None:
        return _decided(
            "skipped_stale",
            "the approved verdict comment no longer exists on the thread",
            bound_comment_id=signal.verdict_comment_id,
        )

    # Re-read revalidation: a retargeted verdict is a DIFFERENT fold. An edit
    # changes no `dedupe_key()` field, so no superseding signal ever arrives —
    # an unconsumed refusal would re-emit every poll forever.
    if parse_body_sha1(verdict.body) != signal.body_sha1:
        return _decided(
            "refused_verdict_edited",
            f"bound verdict's body-sha1 changed since detection "
            f"(signal={signal.body_sha1}, now={parse_body_sha1(verdict.body)})",
            bound_comment_id=signal.verdict_comment_id,
        )

    # --- binding (Mechanics 4) --------------------------------------------------
    bound = verdict if has_proposal_block(verdict.body) else _fallback_binding(
        comments, signal.body_sha1
    )
    if bound is None:
        return _decided(
            "skipped_no_proposal",
            "approved verdict carries no proposal block and no same-digest "
            "verdict with one exists (fallback exhausted)",
            bound_comment_id=signal.verdict_comment_id,
        )

    # --- marker-first (Mechanics 7) --------------------------------------------
    # AFTER binding, BEFORE any digest comparison. The marker means the fold is
    # COMPLETE-OR-TERMINAL: consume with zero writes regardless of the current
    # digest, because a completed fold's after-digest survives an untouched
    # re-triage round and digests alone cannot tell "resume" from "done".
    existing = find_fold_marker(comments, bound.id, bot_login=bot_login)
    if existing is not None:
        # Discriminate the quiet case (a later re-triage bounced the issue back
        # to drafting — a verdict NEWER than the marker exists) from the
        # stranded one (marker committed but the terminal relabel never ran,
        # e.g. a commit-ambiguous `addComment` whose response was lost). Both
        # consume (round-6 decision (b): marker-first never relabels); the
        # stranded one must be LOUD, matching the relabel-failure diagnostic.
        newer_verdict = any(
            is_verdict_comment(c)
            and c.created_at is not None
            and existing.created_at is not None
            and c.created_at > existing.created_at
            for c in comments
        )
        if issue.state == "drafting" and not newer_verdict:
            log(
                "fold apply: STRANDED FOLD — marker present, issue still at "
                "status:drafting, and no verdict is newer than the marker: the "
                "terminal relabel never completed (likely a commit-ambiguous "
                "marker post); relabel to status:triage by hand",
                issue=ident, marker=existing.id,
            )
            return _decided(
                "already_folded_stranded",
                f"fold-applied marker {existing.id} present but the terminal "
                f"relabel never completed; zero writes — relabel by hand",
                bound_comment_id=bound.id,
            )
        return _decided(
            "already_folded",
            f"fold-applied marker {existing.id} already present for the bound "
            f"proposal; zero writes, no relabel",
            bound_comment_id=bound.id,
        )

    payload, error = extract_proposal(bound.body)
    if payload is None:
        return _decided("skipped_malformed_proposal", error or "malformed proposal",
                        bound_comment_id=bound.id)
    after_sha1 = body_digest(payload)

    # --- read 2 (ordinal 1): closed guard + before-digest (Mechanics 10/12) ----
    # ONE call: it returns state, labels AND body in one response.
    try:
        fetched = await tracker.fetch_issue_states_by_ids([signal.issue_id])
    except TrackerError as exc:
        return _retry("read_failed", f"pre-write issue read failed: {exc}",
                      bound_comment_id=bound.id)
    if not fetched:
        return _decided("skipped_stale", "issue is no longer fetchable by node id",
                        bound_comment_id=bound.id)
    current = fetched[0]
    if current.state.lower() == "closed":
        # The relabel helper's own guard fires too late to prevent a partial
        # fold, so the closed check runs before the BODY write.
        return _decided("skipped_closed", "issue was closed before the body write",
                        bound_comment_id=bound.id)
    status_now = [l for l in current.labels if l.startswith("status:")]
    if status_now != [DRAFTING_LABEL]:
        # A human transition landed between the poll's snapshot and this fresh
        # read (e.g. drafting -> decision, or a mid-swap dual-label state).
        # SOLE status required, mirroring the terminal helper's own preemption
        # semantics (`set_sole_status_label` treats anything but exactly one
        # expected status as preempted) — the relabel would refuse at the end,
        # but by then the body would already be rewritten; a stale approval
        # must never modify an issue after a newer human transition.
        return _decided(
            "skipped_state_changed",
            f"issue is not solely at {DRAFTING_LABEL} before the body write "
            f"(status labels now: {', '.join(status_now) or 'none'}); "
            f"the newer transition wins",
            bound_comment_id=bound.id,
        )

    before_now = body_digest(current.description)
    if before_now == signal.body_sha1:
        resume = False
    elif before_now == after_sha1:
        # Re-entry (Mechanics 7): the body write landed, a later step did not.
        resume = True
    else:
        return _decided(
            "refused_clobber",
            f"current body digest {before_now} matches neither the approved "
            f"before-sha1 {signal.body_sha1} nor the fold's after-sha1 "
            f"{after_sha1}; the body changed under the fold",
            bound_comment_id=bound.id,
            before_sha1=before_now,
            after_sha1=after_sha1,
        )

    # `before:` is the digest the OPERATOR approved. In a resume the pre-fold
    # body no longer exists to re-read, so there is no fresh source for it.
    before_sha1 = signal.body_sha1

    if not resume:
        # --- write 1: the body ----------------------------------------------
        try:
            await tracker.update_issue_body(signal.issue_id, payload)
        except TrackerError as exc:
            return _retry("body_write_failed", f"body update failed: {exc}",
                          bound_comment_id=bound.id)

        # --- read 3 (ordinal 2): verify-after-write --------------------------
        try:
            verified = await tracker.fetch_issue_states_by_ids([signal.issue_id])
        except TrackerError as exc:
            # Either nothing was written (digest == before) or everything was
            # (digest == after); Mechanics 7 resumes either way.
            return _retry("read_failed", f"verify re-read failed: {exc}",
                          bound_comment_id=bound.id)
        stored = body_digest(verified[0].description) if verified else None
        if stored != after_sha1:
            # Reported, never claimed (AC 1). CONSUMED because retry is
            # provably futile: a re-emission's digest matches neither
            # before- nor after-sha1 and would just refuse as a clobber one
            # cycle later. No marker, no relabel — the operator adjudicates.
            log("fold apply: verify-after-write DIVERGENCE — body left as "
                "written, no marker posted, no relabel",
                issue_identifier=ident, bound_comment_id=bound.id,
                intended_sha1=after_sha1, stored_sha1=stored)
            return _decided(
                "verify_diverged",
                f"stored body digest {stored} != intended {after_sha1}",
                bound_comment_id=bound.id,
                before_sha1=before_sha1,
                after_sha1=after_sha1,
            )

    # --- write 2: the fold-applied marker -----------------------------------
    # Must precede the relabel: after the relabel the issue leaves the poll set
    # and the marker could never be written at all.
    try:
        await tracker.add_issue_comment(
            signal.issue_id,
            marker_comment_body(
                bound_comment_id=bound.id, before=before_sha1,
                after=after_sha1, issue_url=issue.url,
            ),
        )
    except TrackerError as exc:
        return _retry("marker_post_failed", f"marker comment failed: {exc}",
                      bound_comment_id=bound.id,
                      before_sha1=before_sha1, after_sha1=after_sha1)

    # --- write 3: the terminal relabel ---------------------------------------
    try:
        await tracker.set_sole_status_label(
            signal.issue_id, TRIAGE_LABEL, expected_status=(DRAFTING_LABEL,)
        )
    except TrackerError as exc:
        # TERMINAL-AND-LOGGED (round-6 decision (b)). Retry is not offered:
        # marker-first would suppress any re-emission anyway, and the dominant
        # failure mode is the preemption guard firing on a concurrent human
        # touch — i.e. the operator is already there.
        log("fold apply: RELABEL FAILED after body+marker — issue stays at "
            "status:drafting with a folded body; relabel it by hand",
            issue_identifier=ident, bound_comment_id=bound.id,
            before_sha1=before_sha1, after_sha1=after_sha1, error=str(exc))
        return _decided(
            "relabel_failed", f"body+marker durable; relabel refused: {exc}",
            bound_comment_id=bound.id,
            before_sha1=before_sha1, after_sha1=after_sha1,
        )

    return _decided(
        "resumed" if resume else "applied",
        "fold complete: body, marker, relabel drafting -> triage",
        bound_comment_id=bound.id,
        before_sha1=before_sha1,
        after_sha1=after_sha1,
    )


def _fallback_binding(
    comments: list[IssueComment], body_sha1: str
) -> IssueComment | None:
    """The most recent same-digest verdict WITH a proposal block.

    A proposal-less verdict (a fast-path referral carries the digest but no
    block) binds forward to the proposal the operator's digest names. Tiebreak
    is the house latest-wins rule (`fold.py:181-186`) — latest in the thread,
    consistent with part (a).
    """
    winner: IssueComment | None = None
    ordered = sorted(comments, key=lambda c: (c.created_at or _EPOCH,))
    for comment in ordered:
        if not is_verdict_comment(comment):
            continue
        if parse_body_sha1(comment.body) != body_sha1:
            continue
        if not has_proposal_block(comment.body):
            continue
        winner = comment
    return winner
