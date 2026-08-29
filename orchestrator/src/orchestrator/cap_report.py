"""The cap-hit report (issue #16) — tier 4 of the fail-review verifier's evidence.

The verifier (#31) reads evidence in four tiers, and its tier 4 — "self-reports
LAST — any summary the failed session wrote about its own failure"
(`workflow/WORKFLOW.base.md:424`) — was empty by construction: nothing in the
system produced one. This module builds the artifact that fills it.

Two claims, never one. Every report carries a `self_reported` class (what the
dying session says happened) and a `mechanical` class (what the orchestrator can
see from the break itself), and when they differ **the disagreement is
rendered, not resolved**. Resolving it here would be the whole mistake: the
prompt calls the disagreement line "often the most useful line in the verdict"
(`WORKFLOW.base.md:426-427`), and a module that picked a winner would delete
exactly the signal the verifier exists to weigh — with far less evidence than
the verifier has, since the verifier can also read tiers 1-3 from the workspace.

The mechanical class is a **coarse prior, not a verdict**. It knows one thing:
which ceiling fired. Budget exhaustion means the session ran out of road with no
conclusion reached (`quota`); turn exhaustion means it spent every turn it had
and did not converge (`iteration`). Either can be wrong — a session that spent
its turns going in circles may well have burned its budget doing it — which is
precisely why the self-report is asked for and why disagreement is preserved.

Nothing here decides recovery. `failure_taxonomy.Recovery` is data, #31 routes
on it, and this module only produces the evidence both consume.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .failure_taxonomy import CapFailureClass

CAP_REPORT_HEADING = "## Cap-hit report"

# The two ceilings, named. Both live in `scheduler._worker`'s turn loop.
BUDGET_CAP = "budget"
TURNS_CAP = "turns"

# What the orchestrator can conclude from the ceiling alone. See the module
# docstring: coarse on purpose, and never allowed to overwrite the self-report.
MECHANICAL_CLASS: dict[str, CapFailureClass] = {
    BUDGET_CAP: CapFailureClass.QUOTA,
    TURNS_CAP: CapFailureClass.ITERATION,
}

# Closed set of non-taxonomy values the YAML class fields may carry. #12 parses
# these strings, so they are a wire format alongside the taxonomy's own values
# (`failure_taxonomy` module docstring) — and they are deliberately NOT members
# of `CapFailureClass`: "the session never said" is not a way of failing.
UNAVAILABLE = "unavailable"   # no self-report at all (the fallback path)
UNPARSED = "unparsed"         # a self-report arrived, naming no known class
AGREE = "agree"
DISAGREE = "disagree"

GIT_TIMEOUT_S = 10


def _summary_prompt() -> str:
    """The fixed prompt for the tool-less pass.

    The class menu is DERIVED from `CapFailureClass`, never restated: a prose
    copy of the taxonomy is one reworded bullet away from asking for a class the
    consumer cannot route on, which is the drift `failure_taxonomy` was extracted
    to prevent.
    """
    menu = "\n".join(
        f"  {c.value} — approach still trusted: {c.approach_trusted.value}; "
        f"recovery: {c.recovery.value}"
        for c in CapFailureClass
    )
    return (
        "Your session has hit its cap and is ending now. This is your final "
        "turn.\n\n"
        "You have NO TOOLS. You cannot run commands, read files, or edit "
        "anything, and nothing you write now changes the work. Write your "
        "report from what you already know.\n\n"
        "A reviewer who was not here will read this against the git history, "
        "the issue comments, and your transcript. Your value to them is the "
        "one thing those cannot show: what you were actually trying to do, and "
        "where you actually got to. Be specific and be honest — a report that "
        "disagrees with the mechanical signals is more useful than one that "
        "flatters them.\n\n"
        "Reply with exactly these four lines and nothing else:\n\n"
        "CLASS: <one value from the list below>\n"
        "ATTEMPTED: <one sentence: what you were trying to do>\n"
        "FURTHEST: <one sentence: the furthest state you actually reached>\n"
        "NEXT: <one sentence: the single next action you recommend>\n\n"
        "CLASS must be exactly one of:\n"
        f"{menu}\n"
    )


CAP_SUMMARY_PROMPT = _summary_prompt()

_FIELD_RE = re.compile(
    r"^\s*(CLASS|ATTEMPTED|FURTHEST|NEXT)\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True)
class SelfReport:
    """What the capped session said about itself. Every field may be empty."""

    cap_class: CapFailureClass | None = None
    class_token: str = ""      # what it actually wrote, when that named no class
    attempted: str = ""
    furthest: str = ""
    next_action: str = ""

    @property
    def present(self) -> bool:
        """True when the pass returned anything usable at all.

        A class alone counts, and so does prose alone: a report that names no
        class but says where it got to is still tier-4 evidence, and demoting it
        to the mechanical fallback would throw away the only account of the
        session that exists.
        """
        return bool(
            self.cap_class or self.class_token or self.attempted
            or self.furthest or self.next_action
        )

    @property
    def class_field(self) -> str:
        """The YAML `self_reported:` value — always a closed-set string."""
        if self.cap_class is not None:
            return self.cap_class.value
        return UNPARSED if self.present else UNAVAILABLE


def parse_self_report(text: str) -> SelfReport:
    """Parse the four-line shape `CAP_SUMMARY_PROMPT` asks for.

    Tolerant by design — a model that adds a preamble, wraps the class in
    backticks, or answers three of four fields still produces usable evidence,
    and the alternative to tolerance here is discarding the only self-account
    the system will ever get for this session.
    """
    fields = {
        m.group(1).upper(): m.group(2).strip()
        for m in _FIELD_RE.finditer(text or "")
    }
    raw_class = fields.get("CLASS", "").strip().strip("`'\"* ")
    matched = next(
        (c for c in CapFailureClass if c.value == raw_class.lower()), None)
    return SelfReport(
        cap_class=matched,
        class_token="" if matched else raw_class,
        attempted=fields.get("ATTEMPTED", ""),
        furthest=fields.get("FURTHEST", ""),
        next_action=fields.get("NEXT", ""),
    )


@dataclass(frozen=True)
class MechanicalFacts:
    """State the orchestrator holds at the break, and nothing more.

    Narrowed to HEAD-at-the-break on purpose (issue #16 non-goals): denials,
    tool-event retention and repeated-command detection are the verifier's job,
    read from tiers 1-3 while standing in the workspace. Nothing here crosses
    the runner->orchestrator boundary that did not already cross it.
    """

    cap: str                       # BUDGET_CAP | TURNS_CAP
    turns_spent: int
    max_turns: int
    cost_usd: float
    budget_usd: float | None
    branch: str = ""
    commits_ahead: int | None = None
    last_event: str = ""
    last_event_at: str = ""

    @property
    def cap_class(self) -> CapFailureClass:
        return MECHANICAL_CLASS[self.cap]


def _git_sync(workspace: Path, *args: str) -> str:
    """One read-only git query, run to completion on a worker thread."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True, timeout=GIT_TIMEOUT_S,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


async def _git(workspace: Path, *args: str) -> str:
    """One read-only git query in the workspace; "" on any failure.

    Every caller is building a diagnostic for a session that has already ended,
    so a git failure must degrade the report, never raise into the worker.

    **Deliberately a thread and not `asyncio.create_subprocess_exec`.** An
    asyncio child attaches a subprocess transport to the running loop, and this
    call sits in the one place a worker is most likely to be cancelled — after
    the turn loop broke, while the report is being built. A worker cancelled
    mid-spawn leaves that transport's exit waiter pending, and the loop's own
    shutdown then waits on a task that can never finish: the orchestrator hangs
    instead of the one session ending. A pooled thread running a `subprocess`
    with a hard timeout cannot wedge the loop — the worst case is a bounded
    wait for a thread nobody is listening to any more. A diagnostic that can
    hang the orchestrator is worse than no diagnostic.
    """
    return await asyncio.to_thread(_git_sync, workspace, *args)


async def collect_git_facts(workspace: Path) -> tuple[str, int | None]:
    """(branch, commits ahead of base) for the workspace, best-effort.

    Base is `origin/HEAD` — the clone's own record of the remote default branch
    — rather than a configured branch name, because the orchestrator never
    learns the base: the `before_run` hook does the clone. A workspace where
    `origin/HEAD` does not resolve reports `None` rather than guessing "main".
    """
    branch = await _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    raw = await _git(workspace, "rev-list", "--count", "origin/HEAD..HEAD")
    try:
        commits = int(raw)
    except ValueError:
        commits = None
    return branch, commits


def _yaml_str(value: str) -> str:
    """JSON-quote a scalar. Valid YAML, and immune to a branch name that
    happens to contain a colon, a `#`, or a leading `*`."""
    return json.dumps(value)


def _yaml_block(facts: MechanicalFacts, report: SelfReport) -> str:
    self_reported = report.class_field
    mechanical = facts.cap_class.value
    if self_reported in (UNAVAILABLE, UNPARSED):
        agreement = UNAVAILABLE
    else:
        agreement = AGREE if self_reported == mechanical else DISAGREE
    lines = [
        f"cap: {facts.cap}",
        f"self_reported: {self_reported}",
        f"mechanical: {mechanical}",
        f"agreement: {agreement}",
        f"turns_spent: {facts.turns_spent}",
        f"max_turns: {facts.max_turns}",
        f"cost_usd: {round(facts.cost_usd, 4)}",
        "budget_usd: " + (
            "null" if facts.budget_usd is None else str(facts.budget_usd)),
        f"branch: {_yaml_str(facts.branch)}",
        "commits_ahead: " + (
            "null" if facts.commits_ahead is None else str(facts.commits_ahead)),
        f"last_event: {_yaml_str(facts.last_event)}",
        f"last_event_at: {_yaml_str(facts.last_event_at)}",
    ]
    return "```yaml\n" + "\n".join(lines) + "\n```"


_CAP_PROSE = {
    BUDGET_CAP: "spent its cost ceiling",
    TURNS_CAP: "spent every turn it had",
}


def render_report(
    facts: MechanicalFacts,
    report: SelfReport,
    *,
    summary_error: str = "",
) -> str:
    """The `## Cap-hit report` comment body. Never empty, on any path.

    The mechanical half is built entirely from `facts`, so a failed summary pass
    costs the prose account and nothing else — which matters most on the budget
    path, where a budget-dead session cannot run inference to report its own
    death and the pass is EXPECTED to fail.
    """
    parts = [
        CAP_REPORT_HEADING,
        "",
        f"This session {_CAP_PROSE[facts.cap]} and stopped without handing off. "
        "The account below is the session's own; the block after it is what the "
        "orchestrator could see at the break. They are reported separately on "
        "purpose — where they disagree, that disagreement is the evidence.",
        "",
    ]

    if report.present:
        parts.append("**The session's own account**")
        parts.append("")
        for label, value in (
            ("Attempted", report.attempted),
            ("Furthest state reached", report.furthest),
            ("Recommended next action", report.next_action),
        ):
            parts.append(f"- **{label}:** {value or '_not stated_'}")
        if report.cap_class is not None:
            parts.append(f"- **Self-classified:** `{report.cap_class.value}`")
        elif report.class_token:
            parts.append(
                f"- **Self-classified:** named `{report.class_token}`, which is "
                "not a class in the taxonomy — recorded as `unparsed` below "
                "rather than mapped to a neighbour.")
        else:
            parts.append("- **Self-classified:** _no class stated_")
    else:
        parts.append(
            "**The session's own account: unavailable.** The summary pass did "
            "not return"
            + (f" ({summary_error})" if summary_error else "")
            + ". On the budget path this is the expected case — a session that "
            "has spent its ceiling cannot run inference to report its own "
            "death. Only the mechanical facts below are available.")

    if report.cap_class is not None and report.cap_class is not facts.cap_class:
        parts += [
            "",
            f"**Disagreement.** The session classified this "
            f"`{report.cap_class.value}`; the mechanical signal says "
            f"`{facts.cap_class.value}`. Both are recorded and neither "
            "overrides the other — the mechanical class knows only which "
            "ceiling fired.",
        ]

    parts += [
        "",
        "**Mechanical facts at the break**",
        "",
        _yaml_block(facts, report),
        "",
        "_Tier 4 evidence for a fail-review verifier: check this against the "
        "comment history, the workspace git state, and the transcripts before "
        "trusting it. Written by the Switchboard orchestrator, not by a "
        "human._",
    ]
    return "\n".join(parts)
