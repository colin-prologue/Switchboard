"""Board-state sanity check — stance-independent invalid status labels (issue #52).

**Detect, never revert.** Nothing here writes a `status:*` label, and that is
the load-bearing property rather than an implementation detail: if the system
wrote an incoherent label, the label IS the bug report, and putting it back
tidies the board by deleting the only artifact a debugging session would have
started from. An illegally-promoted ticket already fails to dispatch (the
marker guard refuses it), so a revert would buy cosmetics and cost the whole
loop-guard/re-read apparatus.

**Scope: only what no stance could make legitimate.** `active_states` is a
per-project stance property (AgDR-039) — `base` runs
`triage → todo → in progress → human-review`, `prototype` runs
`todo → in progress → review` — so there is no single legal state machine to
judge a *transition* against. Judging one would mean a rulebook that can
disagree with the dispatcher, which is exactly the two-sources-one-truth
failure AgDR-043 exists to warn about. What survives that narrowing is three
conditions, each judged against the project's OWN config:

1. `undefined-status-label`      — a `status:*` label outside the states this
                                   project's config defines and outside the
                                   durable markers. Nothing dispatches or
                                   closes it, so the ticket is stranded.
2. `multiple-live-status-labels` — two or more labels claiming to BE the state.
                                   `status:parked` is a marker and excluded.
3. `terminal-state-while-open`   — an OPEN ticket labelled with a terminal
                                   state: a half-finished close.

A ticket in a state that is wrong for *its* project but legal for some other
project passes silently. That is the larger class, and it is refused here
rather than deferred — catching it needs the per-project edges above.

Failure is non-fatal by construction: a check that can halt the poll is a
bigger hazard than the states it looks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .log import log
from .tracker import (
    DURABLE_STATUS_MARKERS,
    live_status_labels,
    normalize_status_state,
    status_labels,
)
from .types import Issue, TrackerConfig

CONDITION_UNDEFINED = "undefined-status-label"
CONDITION_MULTIPLE_LIVE = "multiple-live-status-labels"
CONDITION_TERMINAL_WHILE_OPEN = "terminal-state-while-open"

# One comment per issue per condition, durably. The marker keys on the
# condition only — the acceptance contract is "at most one comment per issue
# per condition", so a label set that changes from one invalid shape to another
# invalid shape of the SAME condition does not earn a second comment. Board
# visibility is the point; a per-tick comment would make it a noise surface.
MARKER = "<!-- switchboard:board-sanity condition={condition} -->"


@dataclass(frozen=True)
class Finding:
    """One invalid condition observed on one issue."""

    condition: str
    labels: tuple[str, ...]  # the observed status labels — the evidence
    detail: str  # one sentence, reused verbatim by the log and the comment


class _Tracker(Protocol):
    async def fetch_issue_comments(self, issue_number: str | int) -> list[Any]: ...
    async def add_issue_comment(self, issue_id: str, body: str) -> None: ...


def state_of(label: str) -> str:
    """The workflow state one `status:*` label names.

    Delegates to the tracker's `normalize_status_state` rather than repeating
    the `status:` strip and the dash->space rewrite — a second copy of that
    parse is precisely the drift AgDR-043 was written about.
    """
    return normalize_status_state([label], closed=False)


def defined_labels(cfg: TrackerConfig) -> set[str]:
    """The `status:*` labels this project's config accounts for.

    Config-supplied end to end — no hard-coded state list — which is what keeps
    the check honest across stances: `base` and `prototype` disagree about
    nearly every state, and each is judged against its own declaration.
    """
    known = {state_of(label) for label in DURABLE_STATUS_MARKERS}
    known |= cfg.defined_states()
    return known


def find_invalid_states(issue: Issue, cfg: TrackerConfig) -> list[Finding]:
    """The invalid conditions `issue` exhibits under `cfg`. Pure.

    Callers pass OPEN issues only (condition 3 reads "terminal label while
    open"); `GitHubTracker.fetch_open_issues` is the intended source. Raises on
    a malformed label set — the caller treats that as non-fatal.
    """
    labels = [str(label).strip().lower() for label in issue.labels]
    present = status_labels(labels)
    live = live_status_labels(labels)
    known = defined_labels(cfg)
    findings: list[Finding] = []

    undefined = [label for label in present if state_of(label) not in known]
    if undefined:
        findings.append(
            Finding(
                condition=CONDITION_UNDEFINED,
                labels=tuple(undefined),
                detail=(
                    "carries a status label this project does not define: "
                    + ", ".join(f"`{label}`" for label in undefined)
                    + ". No configured state dispatches or closes it, so the "
                    "ticket cannot move"
                ),
            )
        )

    if len(live) > 1:
        findings.append(
            Finding(
                condition=CONDITION_MULTIPLE_LIVE,
                labels=tuple(live),
                detail=(
                    "carries more than one live status label: "
                    + ", ".join(f"`{label}`" for label in live)
                    + ". At most one may be live. Derivation still resolves "
                    "this deterministically, against the committed state "
                    "precedence — but the precedence exists to rank a HOLD "
                    "above the stage it holds, not to arbitrate between two "
                    "stage labels, so one of these is wrong and only a human "
                    "knows which"
                ),
            )
        )

    terminal = {state.strip().lower() for state in cfg.terminal_states}
    closing = [label for label in live if state_of(label) in terminal]
    if closing:
        findings.append(
            Finding(
                condition=CONDITION_TERMINAL_WHILE_OPEN,
                labels=tuple(closing),
                detail=(
                    "is open but carries a terminal status label: "
                    + ", ".join(f"`{label}`" for label in closing)
                    + ". That is a half-finished close — the label says done, "
                    "the issue says open"
                ),
            )
        )

    return findings


def comment_body(finding: Finding, cfg: TrackerConfig) -> str:
    """The report posted on the ticket. Report only — nothing was changed."""
    known = sorted(defined_labels(cfg))
    return (
        MARKER.format(condition=finding.condition) + "\n"
        f"**Switchboard board-state check: this issue {finding.detail}.**\n\n"
        f"Observed status labels: "
        + ", ".join(f"`{label}`" for label in finding.labels)
        + ".\n\n"
        "Nothing was changed. This check reports and never writes a status "
        "label — the label set is the evidence for whatever wrote it, so it is "
        "left in place. Under this project's configuration the defined states "
        "are: " + ", ".join(f"`{state}`" for state in known) + ".\n\n"
        "Posted once per issue per condition, so this will not repeat while "
        "the condition persists."
    )


async def report_board_state(
    issues: list[Issue],
    cfg: TrackerConfig,
    tracker: _Tracker,
    *,
    reported: set[tuple[str, str]],
) -> list[Finding]:
    """Report every invalid condition across `issues`. Never raises.

    `reported` is the caller-owned in-process memo of `(issue id, condition)`
    pairs already handled, so a second poll tick over the same unchanged state
    costs zero API calls. The durable half is the marker comment: a restart
    empties the memo, and the comment fetch then finds the marker and posts
    nothing.

    Returns the findings it reported (tests and logs; nothing else consumes it).
    """
    reported_now: list[Finding] = []
    for issue in issues:
        try:
            findings = find_invalid_states(issue, cfg)
        except Exception as exc:  # malformed/unreadable label set
            # Non-fatal on purpose: a sanity check that can stop dispatch is a
            # bigger hazard than the states it looks for.
            log("board sanity: could not read this issue's labels; continuing",
                issue_id=getattr(issue, "id", None),
                issue_identifier=getattr(issue, "identifier", None),
                error=str(exc))
            continue

        for finding in findings:
            key = (issue.id, finding.condition)
            if key in reported:
                continue
            log("BOARD STATE INVALID — no status label was written",
                issue_id=issue.id, issue_identifier=issue.identifier,
                condition=finding.condition, labels=",".join(finding.labels),
                detail=finding.detail)
            # Memoized before the write attempt: the log record above is the
            # per-detection record, and re-emitting it every 30s tick on a
            # persistently failing comment write would be its own noise surface.
            reported.add(key)
            reported_now.append(finding)
            try:
                await _post_once(issue, finding, cfg, tracker)
            except Exception as exc:
                log("board sanity: could not post the report comment; the log "
                    "record above stands", issue_id=issue.id,
                    issue_identifier=issue.identifier,
                    condition=finding.condition, error=str(exc))
    return reported_now


async def _post_once(
    issue: Issue, finding: Finding, cfg: TrackerConfig, tracker: _Tracker
) -> None:
    """Post the report unless this issue already carries its marker."""
    marker = MARKER.format(condition=finding.condition)
    for comment in await tracker.fetch_issue_comments(issue.identifier):
        if marker in (getattr(comment, "body", "") or ""):
            return
    await tracker.add_issue_comment(issue.id, comment_body(finding, cfg))
