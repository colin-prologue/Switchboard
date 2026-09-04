"""Operator inbox digest — "what awaits YOU", by body edit (issue #192).

The motivating incident (2026-08-29): the fleet swept the whole active queue,
ten tickets became ten PRs, and the operator learned of it six hours later by
grepping launchd logs. Under AgDR-048 that is the expected failure, not an
anomaly — Switchboard is a single-operator system whose only operator-facing
push channel is the ops log from AgDR-046, and that channel carries exactly one
kind of event: everything stopped. Nothing carried the routine case.

**Body edit, never a comment.** Two house precedents pin this and they pull the
same way:

- Comments are not a safe recurring channel. OBS-022 (2026-07-02): a park
  notification comment bumped the issue's `updatedAt`, which was the unpark
  signal, producing an unbounded park→comment→unpark spend loop that 110
  passing tests never saw because the fake did not model GitHub's
  comment→`updatedAt` echo. A scheduled digest commenting anywhere near a
  tracked issue re-enters that incident class. So the digest writes **no
  comment and no label on any issue**, and the invariant is guarded by a test
  whose fake models that echo.
- Safe body edits have a worked precedent: the fold apply step (`fold_apply.py`,
  issue #126) does read-compare-write over `update_issue_body`, names the TOCTOU
  window as an accepted single-operator residual, and verifies after writing.
  This module has the same shape and the same residual.

**A snapshot, not a feed.** The digest is one always-current body. Unchanged
content produces no write at all, which is what makes a 30-second poll loop
affordable and what keeps the digest issue's own `updatedAt` quiet.

**Where the timestamps live is load-bearing.** The window start (`since`) and
the freshness stamp are rendered ABOVE the compared region, and the next
cycle's watermark rides in the HTML marker line — none of them inside the
content. A timestamp inside the compared content would differ on every render,
force a write every cycle, and destroy the no-write-when-unchanged property
that the whole design rests on. The two values are also genuinely different:
the header names the window the listed artifacts were selected against, while
the marker names where the NEXT cycle starts looking.

**Not a liveness report.** "Is the orchestrator alive" is the health ticket's
beat, and the triage answer (2026-09-03) put this digest in-tick precisely
because a dead fleet accumulates nothing new for an inbox. A stale timestamp
here means "nothing changed", not "nothing ran", and the body says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .cap_report import CAP_REPORT_HEADING
from .log import log
from .tracker import DURABLE_STATUS_MARKERS
from .types import Issue, TrackerConfig

# --- pinned literals ---------------------------------------------------------

# Machine-readable header. `watermark` is where the NEXT cycle starts looking
# for artifacts; it is written on every successful body write.
MARKER_PREFIX = "<!-- switchboard:inbox-digest v1 watermark="
MARKER_SUFFIX = " -->"

# Everything below this sentinel is the COMPARED region. An explicit sentinel
# rather than "skip the first N lines": the header above it is regenerated every
# write (its timestamps move), so the compare boundary has to be unambiguous or
# the digest writes on every cycle and re-enters the noise-surface failure this
# design exists to avoid.
CONTENT_SENTINEL = "<!-- switchboard:inbox-digest:content -->"

# The verifier's grep-able comment anchor (`workflow/WORKFLOW.base.md`, issue
# #31 / AgDR-047). `CAP_REPORT_HEADING` is its sibling and is imported from the
# module that writes it rather than re-spelled here.
FAIL_REVIEW_HEADING = "## Fail-review verdict"

# The first line `scheduler._park` writes. Defined HERE and imported by the
# writer, which is backwards-looking but the only direction with no import
# cycle — the scheduler already depends on this module. The park comment is the
# only durable record of WHY an issue is parked (`status:parked` carries the
# fact, never the reason), so one literal, one source.
PARK_COMMENT_PREFIX = "**Switchboard parked this issue** — "

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def is_parked(issue: Issue) -> bool:
    """Does this issue carry the durable park marker (AgDR-008)?

    Asked through the tracker's marker vocabulary rather than a fresh
    `"status:parked"` literal — `scheduler.PARK_LABEL` is the writer and sits on
    the far side of an import cycle from here, and a second spelling of a label
    is precisely the drift AgDR-043 was written about.
    """
    return any(label in DURABLE_STATUS_MARKERS for label in issue.labels)


# Per-cycle bound on issue-comment fetches. The scan set is already narrow
# (parked issues plus issues touched since the watermark), but a wave that
# touches hundreds of issues must not turn one digest into hundreds of queries.
# Truncation is REPORTED in the body, never silent — a bounded digest that reads
# as complete is worse than one that says what it skipped.
MAX_COMMENT_SCANS = 50


# --- collected state ---------------------------------------------------------

@dataclass(frozen=True)
class IssueRow:
    """One issue in the digest. `note` carries a park reason when there is one."""

    number: str
    title: str
    state: str
    url: str | None = None
    note: str = ""


@dataclass(frozen=True)
class PullRow:
    number: int
    title: str
    url: str | None = None
    is_draft: bool = False
    head_ref: str = ""


@dataclass(frozen=True)
class ArtifactRow:
    """One `## Fail-review verdict` / `## Cap-hit report` comment in the window."""

    issue_number: str
    issue_title: str
    created_at: datetime | None
    url: str | None = None


@dataclass(frozen=True)
class Inbox:
    """Everything one digest cycle found. Rendered, never acted on."""

    awaiting: tuple[IssueRow, ...] = ()
    parked: tuple[IssueRow, ...] = ()
    pulls: tuple[PullRow, ...] = ()
    verdicts: tuple[ArtifactRow, ...] = ()
    cap_reports: tuple[ArtifactRow, ...] = ()
    # Sections whose read failed this cycle. A failed read must never render as
    # an empty section: "no PRs are open" and "we could not ask" are opposite
    # answers and the operator acts differently on each.
    unreadable: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (
            self.awaiting or self.parked or self.pulls
            or self.verdicts or self.cap_reports
            or self.unreadable or self.notes
        )


# --- body format -------------------------------------------------------------

def marker_line(watermark: datetime) -> str:
    return f"{MARKER_PREFIX}{watermark.isoformat()}{MARKER_SUFFIX}"


def parse_watermark(body: str | None) -> datetime | None:
    """The watermark a previous digest left in `body`, if it is still there.

    Durable by construction: it rides in the body the feature writes, so a
    process restart resumes the window rather than re-reporting old artifacts
    (a fresh `now`) or replaying everything (a null window). Returns None for a
    body the operator rewrote past recognition — the caller decides what to fall
    back to.
    """
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(MARKER_PREFIX):
            continue
        if not stripped.endswith(MARKER_SUFFIX):
            return None
        raw = stripped[len(MARKER_PREFIX):-len(MARKER_SUFFIX)].strip()
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def content_of(body: str | None) -> str | None:
    """The compared region of `body`, or None when it carries no sentinel."""
    text = body or ""
    idx = text.find(CONTENT_SENTINEL)
    if idx < 0:
        return None
    rest = text[idx + len(CONTENT_SENTINEL):]
    return rest[1:] if rest.startswith("\n") else rest


def render_body(
    content: str, *, window_start: datetime, watermark: datetime, changed_at: datetime
) -> str:
    """Assemble the whole issue body around an already-rendered content region.

    `window_start` is the window the CONTENT was selected against; `watermark`
    is where the next cycle starts. They differ on every write, and conflating
    them would make the header claim a window the content does not cover.
    """
    return "\n".join([
        marker_line(watermark),
        "",
        "# What awaits you",
        "",
        f"_As of {changed_at.isoformat()} — artifacts below are those posted "
        f"since {window_start.isoformat()}._",
        "",
        "_Switchboard re-checks this every digest cycle and rewrites the body "
        "only when something below changes, so an old timestamp means nothing "
        "has changed — not that nothing is running. This digest reports what "
        "accumulated for you; whether the fleet is alive is a different "
        "question and a different report._",
        "",
        "_Nothing here is a control surface: the digest enumerates and never "
        "unparks, relabels, merges or nudges. Editing this body by hand is "
        "safe — your edit stands until the next cycle re-renders over it._",
        "",
        CONTENT_SENTINEL,
        content,
    ])


def _issue_line(row: IssueRow) -> str:
    where = f"[#{row.number}]({row.url})" if row.url else f"#{row.number}"
    line = f"- {where} `{row.state}` — {row.title}"
    return f"{line} — {row.note}" if row.note else line


def _pull_line(row: PullRow) -> str:
    where = f"[#{row.number}]({row.url})" if row.url else f"#{row.number}"
    draft = " *(draft)*" if row.is_draft else ""
    branch = f" — `{row.head_ref}`" if row.head_ref else ""
    return f"- {where} {row.title}{draft}{branch}"


def _artifact_line(row: ArtifactRow) -> str:
    where = f"[#{row.issue_number}]({row.url})" if row.url else f"#{row.issue_number}"
    when = row.created_at.isoformat() if row.created_at else "(no timestamp)"
    return f"- {where} {row.issue_title} — {when}"


def _section(title: str, lines: list[str], *, empty: str, unreadable: bool) -> list[str]:
    out = [f"## {title}", ""]
    if unreadable:
        out.append(
            "_Could not be read this cycle — this is not the same as empty. "
            "The next cycle retries._"
        )
    elif lines:
        out.extend(lines)
    else:
        out.append(f"_{empty}_")
    out.append("")
    return out


def render_content(inbox: Inbox) -> str:
    """The compared region: everything the operator reads, no timestamps.

    Timestamp-free ON PURPOSE — see the module docstring. Anything varying per
    render belongs above `CONTENT_SENTINEL`, or the no-write-when-unchanged
    guarantee evaporates.
    """
    if inbox.is_empty():
        return (
            "**Nothing awaits you.** No issue is sitting at a gate, no pull "
            "request is open, nothing is parked, and no fail-review verdict or "
            "cap-hit report was posted in the window above.\n"
        )

    out: list[str] = []
    out += _section(
        f"Issues waiting on you ({len(inbox.awaiting)})",
        [_issue_line(r) for r in inbox.awaiting],
        empty="Nothing is sitting at a gate.",
        unreadable="awaiting" in inbox.unreadable,
    )
    out += _section(
        f"Open pull requests ({len(inbox.pulls)})",
        [_pull_line(r) for r in inbox.pulls],
        empty="No pull request is open.",
        unreadable="pulls" in inbox.unreadable,
    )
    out += _section(
        f"Parked ({len(inbox.parked)})",
        [_issue_line(r) for r in inbox.parked],
        empty="Nothing is parked.",
        unreadable="parked" in inbox.unreadable,
    )
    out += _section(
        f"Fail-review verdicts in the window ({len(inbox.verdicts)})",
        [_artifact_line(r) for r in inbox.verdicts],
        empty="No fail-review verdict was posted in the window.",
        unreadable="artifacts" in inbox.unreadable,
    )
    out += _section(
        f"Cap-hit reports in the window ({len(inbox.cap_reports)})",
        [_artifact_line(r) for r in inbox.cap_reports],
        empty="No cap-hit report was posted in the window.",
        unreadable="artifacts" in inbox.unreadable,
    )
    if inbox.notes:
        out += ["## Notes on this digest", ""]
        out += [f"- {note}" for note in inbox.notes]
        out.append("")
    return "\n".join(out)


# --- collection --------------------------------------------------------------

def park_reason(comment_body: str) -> str | None:
    """The reason out of one `_park` notification comment, or None.

    `status:parked` is the durable fact; the reason exists only in this comment
    (AgDR-008), which is why the digest surfaces both — an operator should not
    have to open five issues to learn five budgets ran out.
    """
    first = (comment_body or "").lstrip().splitlines()
    if not first:
        return None
    line = first[0].strip()
    if not line.startswith(PARK_COMMENT_PREFIX):
        return None
    return line[len(PARK_COMMENT_PREFIX):].strip().rstrip(".").strip() or None


def _heading_of(comment_body: str) -> str:
    lines = (comment_body or "").lstrip().splitlines()
    return lines[0].strip() if lines else ""


def _row(issue: Issue, note: str = "") -> IssueRow:
    return IssueRow(
        number=issue.identifier, title=issue.title, state=issue.state,
        url=issue.url, note=note,
    )


def waiting_states(cfg: TrackerConfig) -> set[str]:
    """The states that mean "a human owns this now".

    Config-derived end to end, like `board_sanity.defined_labels`: the gates a
    stance DECLARES plus wherever it hands completed work off. A gate is a state
    nobody dispatches, so this is the same pair of fields the dispatcher reads,
    asked the operator's question instead of the scheduler's.
    """
    states = {s.strip().lower() for s in cfg.gate_states if s and s.strip()}
    handoff = cfg.handoff_state()
    if handoff:
        states.add(handoff)
    return states


async def collect_inbox(
    tracker: Any,
    cfg: TrackerConfig,
    open_issues: list[Issue],
    *,
    watermark: datetime,
    digest_issue_id: str | None = None,
) -> Inbox:
    """Everything one digest reports. Reads only; NEVER raises.

    `open_issues` is the poll tick's already-fetched unfiltered set, so the two
    label-derived sections cost zero extra API calls. Board-sanity's posture
    applies whole: a report that can halt the poll is a bigger hazard than
    whatever it was looking for.
    """
    waiting = waiting_states(cfg)
    scanned = [i for i in open_issues if i.id != digest_issue_id]

    awaiting = tuple(
        _row(i) for i in scanned
        if i.state in waiting and not is_parked(i)
    )
    parked_issues = [i for i in scanned if is_parked(i)]

    unreadable: list[str] = []
    notes: list[str] = []

    pulls: tuple[PullRow, ...] = ()
    try:
        raw_pulls = await tracker.fetch_open_prs_repo_wide()
        pulls = tuple(
            PullRow(
                number=int(p["number"]), title=str(p.get("title") or ""),
                url=p.get("url"), is_draft=bool(p.get("is_draft")),
                head_ref=str(p.get("head_ref") or ""),
            )
            for p in raw_pulls
        )
    except Exception as exc:  # noqa: BLE001 - a read failure degrades, never halts
        log("inbox digest: open-PR read failed; the section reports itself "
            "unreadable rather than empty", error=str(exc))
        unreadable.append("pulls")

    # The comment scan set: parked issues need their reason on EVERY cycle (the
    # reason is not on the label), and anything touched since the watermark may
    # carry a new verdict or cap-hit report — a comment write bumps the issue's
    # `updatedAt`, which is exactly the echo OBS-022 was about, used here as a
    # read filter instead of a control signal.
    touched = [
        i for i in scanned
        if i.updated_at is not None and i.updated_at > watermark
    ]
    by_id: dict[str, Issue] = {}
    for issue in (*parked_issues, *touched):
        by_id.setdefault(issue.id, issue)
    scan = sorted(
        by_id.values(),
        key=lambda i: (i.updated_at or _EPOCH, i.identifier),
        reverse=True,
    )
    if len(scan) > MAX_COMMENT_SCANS:
        notes.append(
            f"{len(scan) - MAX_COMMENT_SCANS} issue(s) were not scanned for "
            f"new verdicts, cap-hit reports or park reasons this cycle (bounded "
            f"at {MAX_COMMENT_SCANS} per digest); the most recently updated "
            f"were preferred."
        )
        scan = scan[:MAX_COMMENT_SCANS]

    reasons: dict[str, str] = {}
    verdicts: list[ArtifactRow] = []
    cap_reports: list[ArtifactRow] = []
    read_failures = 0
    for issue in scan:
        try:
            comments = await tracker.fetch_issue_comments(issue.identifier)
        except Exception as exc:  # noqa: BLE001 - per-issue, never fatal
            read_failures += 1
            log("inbox digest: could not read one issue's comments; continuing",
                issue_identifier=issue.identifier, error=str(exc))
            continue
        for comment in comments:
            body = getattr(comment, "body", "") or ""
            reason = park_reason(body)
            if reason is not None:
                reasons[issue.id] = reason  # latest wins (oldest-first order)
                continue
            created = getattr(comment, "created_at", None)
            if created is None or created <= watermark:
                continue
            heading = _heading_of(body)
            row = ArtifactRow(
                issue_number=issue.identifier, issue_title=issue.title,
                created_at=created, url=issue.url,
            )
            if heading == FAIL_REVIEW_HEADING:
                verdicts.append(row)
            elif heading == CAP_REPORT_HEADING:
                cap_reports.append(row)
    if read_failures:
        unreadable.append("artifacts")
        notes.append(
            f"{read_failures} issue(s) could not be read for new verdicts, "
            f"cap-hit reports or park reasons this cycle."
        )

    parked = tuple(
        _row(i, reasons.get(i.id, "reason not found on the issue"))
        for i in parked_issues
    )
    def key(row: ArtifactRow) -> tuple[datetime, str]:
        return (row.created_at or _EPOCH, row.issue_number)

    return Inbox(
        awaiting=awaiting,
        parked=parked,
        pulls=pulls,
        verdicts=tuple(sorted(verdicts, key=key)),
        cap_reports=tuple(sorted(cap_reports, key=key)),
        unreadable=tuple(unreadable),
        notes=tuple(notes),
    )


# --- the cycle ---------------------------------------------------------------

@dataclass(frozen=True)
class DigestOutcome:
    """What one digest cycle did. `watermark` is set only when a write landed."""

    status: str
    detail: str
    wrote: bool = False
    watermark: datetime | None = None
    inbox: Inbox | None = field(default=None, compare=False)


async def run_digest(
    tracker: Any,
    cfg: TrackerConfig,
    open_issues: list[Issue],
    *,
    digest_issue_id: str,
    now: datetime,
    fallback_watermark: datetime | None = None,
) -> DigestOutcome:
    """One read-compare-write cycle over the digest issue. Never raises.

    `now` is captured BEFORE the gather by the caller and becomes the new
    watermark, so an artifact posted while this cycle was reading is re-reported
    next cycle rather than dropped. Re-reporting is the safe direction; the
    window silently closing over an unread fail-review verdict is not.

    Read-compare-write, not compare-and-swap: GitHub offers no conditional
    update, so the gap between the pre-write re-read and the write itself is the
    same accepted single-operator residual `fold_apply` names. What the re-read
    DOES buy is the gather-length window — the expensive part of the cycle —
    which is where a concurrent edit realistically lands.
    """
    try:
        fetched = await tracker.fetch_issue_states_by_ids([digest_issue_id])
    except Exception as exc:  # noqa: BLE001
        return DigestOutcome("read_failed", f"digest issue read failed: {exc}")
    if not fetched:
        return DigestOutcome(
            "read_failed", "digest issue is not fetchable by node id")
    before_body = fetched[0].description or ""
    window_start = parse_watermark(before_body) or fallback_watermark or now

    inbox = await collect_inbox(
        tracker, cfg, open_issues,
        watermark=window_start, digest_issue_id=digest_issue_id,
    )
    content = render_content(inbox)

    if content_of(before_body) == content:
        # The whole point of the snapshot model: a quiet board costs one read.
        # The watermark deliberately does NOT advance — the body still describes
        # the window it was rendered against, and advancing it here would close
        # a window over artifacts nobody has been shown.
        return DigestOutcome("unchanged", "content is unchanged; no write",
                             inbox=inbox)

    try:
        again = await tracker.fetch_issue_states_by_ids([digest_issue_id])
    except Exception as exc:  # noqa: BLE001
        return DigestOutcome("read_failed", f"pre-write re-read failed: {exc}",
                             inbox=inbox)
    if not again or (again[0].description or "") != before_body:
        log("inbox digest: the digest body changed while this cycle was "
            "reading; refusing to clobber it, retrying next cycle",
            issue_id=digest_issue_id)
        return DigestOutcome(
            "refused_clobber",
            "the digest body changed under the read; nothing was written",
            inbox=inbox,
        )

    body = render_body(
        content, window_start=window_start, watermark=now, changed_at=now)
    try:
        await tracker.update_issue_body(digest_issue_id, body)
    except Exception as exc:  # noqa: BLE001
        return DigestOutcome("write_failed", f"body update failed: {exc}",
                             inbox=inbox)

    # Verify-after-write (fold_apply precedent). It cannot catch a clobber — it
    # compares STORED against INTENDED — but it does catch a write that did not
    # land as issued, and on divergence the watermark must NOT advance or the
    # next cycle closes a window over artifacts that were never rendered.
    try:
        stored = await tracker.fetch_issue_states_by_ids([digest_issue_id])
    except Exception as exc:  # noqa: BLE001
        log("inbox digest: verify re-read failed after a write that may have "
            "landed; the watermark is not advanced, so the next cycle re-reports "
            "this window", issue_id=digest_issue_id, error=str(exc))
        return DigestOutcome("verify_unread", f"verify re-read failed: {exc}",
                             wrote=True, inbox=inbox)
    if not stored or (stored[0].description or "") != body:
        log("inbox digest: verify-after-write DIVERGENCE — the stored body is "
            "not the one issued; the watermark is not advanced",
            issue_id=digest_issue_id)
        return DigestOutcome(
            "verify_diverged", "stored body != the body issued",
            wrote=True, inbox=inbox,
        )
    return DigestOutcome("written", "digest body rewritten", wrote=True,
                         watermark=now, inbox=inbox)


__all__ = [
    "ArtifactRow", "CONTENT_SENTINEL", "DigestOutcome", "FAIL_REVIEW_HEADING",
    "Inbox", "IssueRow", "MARKER_PREFIX", "MAX_COMMENT_SCANS",
    "PARK_COMMENT_PREFIX", "PullRow", "collect_inbox",
    "content_of", "marker_line", "park_reason", "parse_watermark",
    "render_body", "render_content", "run_digest", "waiting_states",
]
