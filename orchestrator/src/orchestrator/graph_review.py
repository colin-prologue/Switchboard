"""graph-review analyzer (Phase 1, MVP).

implements: self/.switchboard/intents/graph-review.md + AgDR-009.

A manually-invoked, read-only pass that reads the open ticket board — bodies,
comments, native `blockedBy` edges, milestones — plus recently merged PRs, and
writes evidence-cited, keyed proposals to a single rolling **Graph Review**
issue. Proposals-only: no graph mutation except writing that one issue.

Structure:
- Detection (`detect_*`) is pure: `Board` -> `list[Proposal]`. No I/O.
- Structural proposals (merge/split/resequence) pass an injected refute
  sub-check; mechanical proposals skip it.
- The ledger is rendered to / parsed from the issue body so a re-run reads its
  own prior output and never re-raises an `accepted`/`dismissed` key.
- `GraphReviewGitHub` is the only I/O; it reuses `GitHubTracker.graphql`.

Native edges are read ONLY via `blockedBy` (never `trackedIssues` /
`trackedInIssues`) — see AC-6 / the intent's binding constraints.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from orchestrator.log import log
from orchestrator.tracker import GitHubTracker
from orchestrator.workflow import Config, load_workflow

# --- ledger markers ----------------------------------------------------------

LEDGER_MARKER = "<!-- switchboard:graph-review -->"
LEDGER_TITLE = "Graph Review"
_PROPOSAL_OPEN = "<!-- gr:proposal key={key} -->"
_PROPOSAL_CLOSE = "<!-- /gr:proposal -->"
_PROPOSAL_RE = re.compile(
    r"<!-- gr:proposal key=(?P<key>[^\s]+) -->\n(?P<block>.*?)\n<!-- /gr:proposal -->",
    re.DOTALL,
)

VALID_STATES = ("open", "accepted", "dismissed")

# Structural proposals are judgment calls: a skeptic must fail to refute them
# before they are written. Mechanical proposals skip the refute pass.
REFUTED_CATEGORIES = frozenset({"merge", "split", "resequence"})

# Hard-dependency prose. Soft "see also #N" is deliberately NOT matched.
_DEP_RE = re.compile(r"(?:blocked by|depends? on|requires?)\s+#(\d+)", re.IGNORECASE)
_SUPERSEDE_RE = re.compile(
    r"(?:supersedes?|replaces?|obsoletes?|invalidates?)\s+#(\d+)", re.IGNORECASE
)
_ASSUMPTION_RE = re.compile(r"\bassum(?:e|es|ption|ptions)\b", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"#(\d+)")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[[ xX]\]", re.MULTILINE)

_STOPWORDS = frozenset(
    "a an the and or of to for in on with add fix update support via issue "
    "ticket feat feature chore docs test refactor".split()
)
_MERGE_TITLE_JACCARD = 0.5
_SPLIT_MIN_CHECKBOXES = 6


# --- domain (graph-review local; richer than the scheduler's Issue) ----------


@dataclass
class Comment:
    author: str | None
    body: str
    url: str | None


@dataclass
class MilestoneRef:
    title: str
    due_on: datetime | None


@dataclass
class BoardIssue:
    number: int
    node_id: str
    title: str
    body: str
    state: str  # "open" | "closed"
    labels: list[str]
    milestone: MilestoneRef | None
    blocked_by: list[int]  # native blocker numbers (from `blockedBy`)
    blocker_states: dict[int, str]  # number -> "open" | "closed"
    comments: list[Comment]
    url: str | None


@dataclass
class MergedPR:
    number: int
    title: str
    body: str
    merge_sha: str | None
    merged_at: datetime | None
    closes: list[int]  # issue numbers this PR closed/references natively


@dataclass
class Board:
    issues: list[BoardIssue]  # OPEN issues under review (ledger excluded)
    merged_prs: list[MergedPR]
    repo_id: str | None = None
    # >100 open issues: analysis stays capped to the first page (Phase 1), but
    # ledger DISCOVERY must not be — run_analysis pages on from end_cursor.
    issues_truncated: bool = False
    end_cursor: str | None = None

    def by_number(self) -> dict[int, BoardIssue]:
        return {i.number: i for i in self.issues}


@dataclass
class Proposal:
    key: str  # "category:sorted-issue-list", e.g. "edge:16,31"
    category: str
    action: str
    evidence: list[str] = field(default_factory=list)
    state: str = "open"


@dataclass
class RefuteVerdict:
    refuted: bool
    reason: str = ""


# refuter: given a structural proposal and the board, try to disprove it.
RefuteFn = Callable[[Proposal, Board], RefuteVerdict]


# --- key helpers -------------------------------------------------------------


def _pair_key(category: str, a: int, b: int) -> str:
    lo, hi = sorted((a, b))
    return f"{category}:{lo},{hi}"


def _single_key(category: str, n: int) -> str:
    return f"{category}:{n}"


def _snippet(text: str, around: int, width: int = 60) -> str:
    """A one-line quote centered on offset `around`, collapsed whitespace."""
    lo = max(0, around - width)
    hi = min(len(text), around + width)
    return " ".join(text[lo:hi].split())


# --- detectors (pure) --------------------------------------------------------


def detect_edges(board: Board) -> list[Proposal]:
    """Prose hard-dependency with no native `blockedBy` edge for the pair."""
    by_num = board.by_number()
    out: dict[str, Proposal] = {}
    for issue in board.issues:
        sources = [("body", issue.body)] + [
            ("comment", c.body) for c in issue.comments
        ]
        for where, text in sources:
            for m in _DEP_RE.finditer(text or ""):
                target = int(m.group(1))
                if target == issue.number or target not in by_num:
                    continue  # only actionable edges to a currently-open issue
                # A native edge in EITHER direction means the pair is linked.
                if target in issue.blocked_by or issue.number in by_num[target].blocked_by:
                    continue
                key = _pair_key("edge", issue.number, target)
                if key in out:
                    continue
                quote = _snippet(text, m.start())
                out[key] = Proposal(
                    key=key,
                    category="edge",
                    action=f"Add native blockedBy edge: #{issue.number} blocked by #{target}",
                    evidence=[f'#{issue.number} {where}: "{quote}"'],
                )
    return list(out.values())


def detect_milestones(board: Board) -> list[Proposal]:
    """Issue with a milestoned native blocker but no milestone of its own."""
    by_num = board.by_number()
    out: list[Proposal] = []
    for issue in board.issues:
        if issue.milestone is not None or not issue.blocked_by:
            continue
        for b in issue.blocked_by:
            blocker = by_num.get(b)
            if blocker and blocker.milestone is not None:
                out.append(
                    Proposal(
                        key=_single_key("milestone", issue.number),
                        category="milestone",
                        action=f"Assign #{issue.number} a milestone "
                        f"(blocker #{b} is in milestone '{blocker.milestone.title}')",
                        evidence=[f"#{b} milestone: '{blocker.milestone.title}'"],
                    )
                )
                break
    return out


def detect_resequence(board: Board) -> list[Proposal]:
    """A native blocker scheduled in a LATER milestone than what it blocks."""
    by_num = board.by_number()
    out: list[Proposal] = []
    for issue in board.issues:
        if issue.milestone is None or issue.milestone.due_on is None:
            continue
        for b in issue.blocked_by:
            blocker = by_num.get(b)
            if (
                blocker
                and blocker.milestone is not None
                and blocker.milestone.due_on is not None
                and blocker.milestone.due_on > issue.milestone.due_on
            ):
                out.append(
                    Proposal(
                        key=_pair_key("resequence", issue.number, b),
                        category="resequence",
                        action=f"Resequence: blocker #{b} ('{blocker.milestone.title}') "
                        f"is scheduled after #{issue.number} ('{issue.milestone.title}')",
                        evidence=[
                            f"#{b} milestone '{blocker.milestone.title}' due after "
                            f"#{issue.number} milestone '{issue.milestone.title}'"
                        ],
                    )
                )
    return out


def _title_tokens(title: str) -> set[str]:
    toks = re.findall(r"[a-z0-9]+", title.lower())
    return {t for t in toks if t not in _STOPWORDS and len(t) > 2}


def detect_merges(board: Board) -> list[Proposal]:
    """Two open issues with high title overlap that cross-reference each other."""
    out: list[Proposal] = []
    issues = board.issues
    for i in range(len(issues)):
        a = issues[i]
        a_tokens = _title_tokens(a.title)
        a_refs = {int(x) for x in _ISSUE_REF_RE.findall(a.body or "")}
        for j in range(i + 1, len(issues)):
            b = issues[j]
            cross_ref = b.number in a_refs or a.number in {
                int(x) for x in _ISSUE_REF_RE.findall(b.body or "")
            }
            if not cross_ref:
                continue
            b_tokens = _title_tokens(b.title)
            union = a_tokens | b_tokens
            if not union:
                continue
            jaccard = len(a_tokens & b_tokens) / len(union)
            if jaccard < _MERGE_TITLE_JACCARD:
                continue
            out.append(
                Proposal(
                    key=_pair_key("merge", a.number, b.number),
                    category="merge",
                    action=f"Consider merging #{a.number} and #{b.number} "
                    f"(near-duplicate scope; title overlap {jaccard:.0%})",
                    evidence=[
                        f"#{a.number} '{a.title}'",
                        f"#{b.number} '{b.title}'",
                        "issues cross-reference each other",
                    ],
                )
            )
    return out


def detect_splits(board: Board) -> list[Proposal]:
    """One issue enumerating many independent deliverables (checkbox count)."""
    out: list[Proposal] = []
    for issue in board.issues:
        n = len(_CHECKBOX_RE.findall(issue.body or ""))
        if n >= _SPLIT_MIN_CHECKBOXES:
            out.append(
                Proposal(
                    key=_single_key("split", issue.number),
                    category="split",
                    action=f"Consider splitting #{issue.number} "
                    f"({n} independent deliverables enumerated)",
                    evidence=[f"#{issue.number} body enumerates {n} checkbox items"],
                )
            )
    return out


def detect_stale_assumptions(board: Board) -> list[Proposal]:
    """A merged PR that supersedes an open issue, or references it while the
    issue states an explicit assumption."""
    by_num = board.by_number()
    out: dict[str, Proposal] = {}
    for pr in board.merged_prs:
        pr_text = f"{pr.title}\n{pr.body or ''}"
        superseded = {int(x) for x in _SUPERSEDE_RE.findall(pr_text)}
        referenced = set(pr.closes) | {int(x) for x in _ISSUE_REF_RE.findall(pr_text)}
        sha = pr.merge_sha or "unknown"
        for num in referenced:
            issue = by_num.get(num)
            if issue is None:  # only open issues on the board
                continue
            issue_text = issue.body + "\n" + "\n".join(c.body for c in issue.comments)
            asserts_assumption = bool(_ASSUMPTION_RE.search(issue_text))
            if num not in superseded and not asserts_assumption:
                continue
            key = _single_key("stale-assumption", num)
            if key in out:
                continue
            why = "supersedes it" if num in superseded else "may invalidate its stated assumption"
            out[key] = Proposal(
                key=key,
                category="stale-assumption",
                action=f"Re-check #{num}: merged PR #{pr.number} {why}",
                evidence=[f"PR #{pr.number} (sha {sha[:10]}) references #{num}"],
            )
    return list(out.values())


def detect_promotable(board: Board) -> list[Proposal]:
    """Every native blocker of an open issue is closed -> promotable."""
    out: list[Proposal] = []
    for issue in board.issues:
        if not issue.blocked_by:
            continue
        states = [issue.blocker_states.get(b, "open") for b in issue.blocked_by]
        if all(s == "closed" for s in states):
            out.append(
                Proposal(
                    key=_single_key("promotable", issue.number),
                    category="promotable",
                    action=f"Promotable: all blockers of #{issue.number} are closed; "
                    f"move it to status:todo",
                    evidence=[f"blockers {issue.blocked_by} all closed"],
                )
            )
    return out


_DETECTORS = (
    detect_edges,
    detect_milestones,
    detect_resequence,
    detect_merges,
    detect_splits,
    detect_stale_assumptions,
    detect_promotable,
)


def detect_all(board: Board) -> list[Proposal]:
    proposals: list[Proposal] = []
    for det in _DETECTORS:
        proposals.extend(det(board))
    return proposals


def apply_refutation(
    proposals: list[Proposal], board: Board, refuter: RefuteFn
) -> list[Proposal]:
    """Drop structural proposals the refuter disproves; mechanical ones pass
    through untouched (the refuter is never called on them)."""
    kept: list[Proposal] = []
    for p in proposals:
        if p.category not in REFUTED_CATEGORIES:
            kept.append(p)
            continue
        try:
            verdict = refuter(p, board)
        except Exception as exc:  # a skeptic that cannot run -> drop (conservative)
            log("graph-review refuter error; dropping structural proposal",
                key=p.key, error=str(exc))
            continue
        if verdict.refuted:
            log("graph-review proposal refuted", key=p.key, reason=verdict.reason)
            continue
        kept.append(p)
    return kept


# --- refuters ----------------------------------------------------------------


def never_refute(_proposal: Proposal, _board: Board) -> RefuteVerdict:
    """Test/back-stop refuter that disproves nothing."""
    return RefuteVerdict(refuted=False)


def claude_refuter(command: str = "claude -p") -> RefuteFn:
    """Default shipped refuter: a `claude -p` skeptic that tries to disprove the
    relationship. Returns a callable. Any failure to obtain a clear verdict is
    treated as 'refuted' by `apply_refutation` (drop-on-doubt)."""

    def _refute(proposal: Proposal, _board: Board) -> RefuteVerdict:
        prompt = (
            "You are a skeptic reviewing a graph-review proposal about a ticket "
            "board. Try to DISPROVE it. Reply with a single JSON object "
            '{"refuted": true|false, "reason": "..."} and nothing else.\n\n'
            f"Proposal ({proposal.category}): {proposal.action}\n"
            f"Evidence: {proposal.evidence}\n"
        )
        proc = subprocess.run(  # noqa: S603 - fixed command, no shell
            command.split() + [prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode != 0:
            return RefuteVerdict(refuted=True, reason="refuter subprocess failed")
        match = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
        if not match:
            return RefuteVerdict(refuted=True, reason="no verdict parsed")
        data = json.loads(match.group(0))
        return RefuteVerdict(
            refuted=bool(data.get("refuted", True)), reason=str(data.get("reason", ""))
        )

    return _refute


# --- ledger render / parse ---------------------------------------------------


def render_ledger(proposals: list[Proposal]) -> str:
    """Render the full Graph Review issue body (marker + proposals)."""
    order = {"open": 0, "accepted": 1, "dismissed": 2}
    ordered = sorted(proposals, key=lambda p: (order.get(p.state, 0), p.key))
    lines = [
        LEDGER_MARKER,
        "# Graph Review",
        "",
        "_Automated, evidence-cited proposals from the graph-review analyzer "
        "(read-only; Phase 1). To dispose of a proposal, change its "
        "`- **state:**` line to `accepted` or `dismissed`; the analyzer will "
        "never re-raise a disposed key._",
        "",
        f"_Proposals: {len(ordered)} "
        f"({sum(1 for p in ordered if p.state == 'open')} open)._",
        "",
        "## Proposals",
        "",
    ]
    if not ordered:
        lines.append("_No proposals._")
    for p in ordered:
        lines.append(_PROPOSAL_OPEN.format(key=p.key))
        lines.append(f"### `{p.key}` — {p.category}")
        lines.append(f"- **state:** {p.state}")
        lines.append(f"- **action:** {p.action}")
        lines.append("- **evidence:**")
        for ev in p.evidence:
            lines.append(f"  - {ev}")
        lines.append(_PROPOSAL_CLOSE)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_ledger(body: str | None) -> list[Proposal]:
    """Recover proposals (key, state, action, evidence) from an existing body."""
    if not body:
        return []
    out: list[Proposal] = []
    for m in _PROPOSAL_RE.finditer(body):
        key = m.group("key")
        block = m.group("block")
        category = key.split(":", 1)[0]
        state = "open"
        action = ""
        evidence: list[str] = []
        in_evidence = False
        for raw in block.splitlines():
            line = raw.strip()
            sm = re.match(r"- \*\*state:\*\*\s*(\w+)", line)
            am = re.match(r"- \*\*action:\*\*\s*(.+)", line)
            if sm:
                candidate = sm.group(1).lower()
                state = candidate if candidate in VALID_STATES else "open"
                in_evidence = False
            elif am:
                action = am.group(1).strip()
                in_evidence = False
            elif line.startswith("- **evidence:**"):
                in_evidence = True
            elif in_evidence and line.startswith("- "):
                evidence.append(line[2:].strip())
        out.append(Proposal(key=key, category=category, action=action,
                             evidence=evidence, state=state))
    return out


def reconcile(prior: list[Proposal], fresh: list[Proposal]) -> list[Proposal]:
    """Merge a fresh detection run with the prior ledger.

    - `accepted`/`dismissed` prior proposals are preserved verbatim and their
      keys are NEVER re-raised, even if the detector regenerates them.
    - a fresh proposal whose key was open before stays open (content refreshed).
    - prior *open* proposals not regenerated are dropped (the condition
      resolved).
    """
    prior_by_key = {p.key: p for p in prior}
    decided = {k: p for k, p in prior_by_key.items() if p.state in ("accepted", "dismissed")}
    result: list[Proposal] = list(decided.values())
    for p in fresh:
        if p.key in decided:
            continue  # never re-raise a disposed key
        p.state = "open"
        result.append(p)
    return result


# --- GitHub I/O (the only writes: the ledger issue) --------------------------

_BOARD_QUERY = """
query($owner: String!, $name: String!, $prCount: Int!) {
  repository(owner: $owner, name: $name) {
    id
    issues(first: 100, states: [OPEN], orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes {
        id
        number
        title
        body
        url
        state
        labels(first: 50) { nodes { name } }
        milestone { title dueOn }
        blockedBy(first: 20) { nodes { number state } }
        comments(first: 50) { nodes { author { login } body url } }
      }
      pageInfo { hasNextPage endCursor }
    }
    pullRequests(first: $prCount, states: [MERGED], orderBy: {field: UPDATED_AT, direction: DESC}) {
      nodes {
        number
        title
        body
        mergedAt
        mergeCommit { oid }
        closingIssuesReferences(first: 20) { nodes { number } }
      }
    }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation($repositoryId: ID!, $title: String!, $body: String!) {
  createIssue(input: {repositoryId: $repositoryId, title: $title, body: $body}) {
    issue { id number url }
  }
}
"""

_UPDATE_ISSUE_BODY_MUTATION = """
mutation($id: ID!, $body: String!) {
  updateIssue(input: {id: $id, body: $body}) {
    issue { id number }
  }
}
"""

# Ledger discovery past the board's first page (Codex PR #41 P1): a light
# scan — id/number/title/body only — so idempotency holds on 100+-issue repos
# without paying full-board pagination (a Phase-1 non-goal for ANALYSIS only).
_LEDGER_SCAN_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, states: [OPEN], orderBy: {field: CREATED_AT, direction: ASC}, after: $after) {
      nodes { id number title body }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class GraphReviewGitHub:
    """Read the board + write the single Graph Review issue, over the tracker's
    vetted transport. This is the analyzer's ONLY I/O surface."""

    def __init__(self, tracker: GitHubTracker, repo: str, pr_count: int = 30) -> None:
        self._tracker = tracker
        self._owner, _, self._name = repo.partition("/")
        self._pr_count = pr_count

    async def fetch_board(self) -> Board:
        data = await self._tracker.graphql(
            _BOARD_QUERY,
            {"owner": self._owner, "name": self._name, "prCount": self._pr_count},
        )
        repo = data.get("repository") or {}
        issues_conn = repo.get("issues") or {}
        page_info = issues_conn.get("pageInfo") or {}
        truncated = bool(page_info.get("hasNextPage"))
        if truncated:
            log("graph-review: board exceeds 100 open issues; only the first 100 "
                "are analyzed this run (no pagination in Phase 1); ledger "
                "discovery pages through the rest")
        issues = [self._normalize_issue(n) for n in (issues_conn.get("nodes") or [])]
        prs = [self._normalize_pr(n) for n in ((repo.get("pullRequests") or {}).get("nodes") or [])]
        return Board(issues=issues, merged_prs=prs, repo_id=repo.get("id"),
                     issues_truncated=truncated, end_cursor=page_info.get("endCursor"))

    async def scan_for_ledger(self, after: str | None) -> BoardIssue | None:
        """Page through the remaining open issues looking for the ledger
        (Codex PR #41 P1). Same two-tier match as find_ledger: a marker hit
        wins immediately; an exact-title hit is remembered and returned only
        if no marker is found by exhaustion."""
        title_match: BoardIssue | None = None
        while True:
            data = await self._tracker.graphql(
                _LEDGER_SCAN_QUERY,
                {"owner": self._owner, "name": self._name, "after": after},
            )
            conn = ((data.get("repository") or {}).get("issues")) or {}
            for raw in conn.get("nodes") or []:
                issue = BoardIssue(
                    number=raw["number"], node_id=raw["id"],
                    title=raw.get("title") or "", body=raw.get("body") or "",
                    state="open", labels=[], milestone=None, blocked_by=[],
                    blocker_states={}, comments=[], url=None,
                )
                if LEDGER_MARKER in issue.body:
                    return issue
                if title_match is None and issue.title.strip() == LEDGER_TITLE:
                    title_match = issue
            page_info = conn.get("pageInfo") or {}
            after = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or after is None:
                return title_match

    @staticmethod
    def _normalize_issue(raw: dict[str, Any]) -> BoardIssue:
        labels = [n["name"].strip().lower() for n in ((raw.get("labels") or {}).get("nodes") or [])]
        ms = raw.get("milestone")
        milestone = MilestoneRef(title=ms["title"], due_on=_parse_iso(ms.get("dueOn"))) if ms else None
        blocker_nodes = (raw.get("blockedBy") or {}).get("nodes") or []
        blocked_by = [n["number"] for n in blocker_nodes if n.get("number") is not None]
        blocker_states = {
            n["number"]: ("closed" if n.get("state") == "CLOSED" else "open")
            for n in blocker_nodes
            if n.get("number") is not None
        }
        comments = [
            Comment(
                author=(c.get("author") or {}).get("login"),
                body=c.get("body") or "",
                url=c.get("url"),
            )
            for c in ((raw.get("comments") or {}).get("nodes") or [])
        ]
        return BoardIssue(
            number=raw["number"],
            node_id=raw["id"],
            title=raw.get("title") or "",
            body=raw.get("body") or "",
            state="closed" if raw.get("state") == "CLOSED" else "open",
            labels=labels,
            milestone=milestone,
            blocked_by=blocked_by,
            blocker_states=blocker_states,
            comments=comments,
            url=raw.get("url"),
        )

    @staticmethod
    def _normalize_pr(raw: dict[str, Any]) -> MergedPR:
        closes = [
            n["number"]
            for n in ((raw.get("closingIssuesReferences") or {}).get("nodes") or [])
            if n.get("number") is not None
        ]
        return MergedPR(
            number=raw["number"],
            title=raw.get("title") or "",
            body=raw.get("body") or "",
            merge_sha=(raw.get("mergeCommit") or {}).get("oid"),
            merged_at=_parse_iso(raw.get("mergedAt")),
            closes=closes,
        )

    async def create_ledger(self, repo_id: str, body: str) -> None:
        await self._tracker.graphql(
            _CREATE_ISSUE_MUTATION,
            {"repositoryId": repo_id, "title": LEDGER_TITLE, "body": body},
        )

    async def update_ledger(self, node_id: str, body: str) -> None:
        await self._tracker.graphql(
            _UPDATE_ISSUE_BODY_MUTATION, {"id": node_id, "body": body}
        )


def find_ledger(board: Board) -> BoardIssue | None:
    """Locate the rolling Graph Review issue (idempotent): by marker first, then
    by exact title."""
    for issue in board.issues:
        if LEDGER_MARKER in (issue.body or ""):
            return issue
    for issue in board.issues:
        if issue.title.strip() == LEDGER_TITLE:
            return issue
    return None


@dataclass
class RunSummary:
    created: bool
    total: int
    open: int
    refuted_dropped: int


async def run_analysis(
    io: GraphReviewGitHub,
    refuter: RefuteFn,
    *,
    dry_run: bool = False,
) -> tuple[RunSummary, str]:
    """One analyzer pass. Returns (summary, rendered ledger body)."""
    board = await io.fetch_board()

    ledger = find_ledger(board)
    if ledger is None and board.issues_truncated:
        # The ledger may sit past the first page (CREATED_AT ASC puts it late
        # in old repos). Creating without scanning would duplicate the ledger
        # on every run — the one thing its design promises not to do.
        ledger = await io.scan_for_ledger(board.end_cursor)
    if ledger is not None:  # exclude the ledger itself from analysis
        board.issues = [i for i in board.issues if i.number != ledger.number]

    fresh = detect_all(board)
    before = len(fresh)
    fresh = apply_refutation(fresh, board, refuter)
    refuted_dropped = before - len(fresh)

    prior = parse_ledger(ledger.body if ledger else None)
    final = reconcile(prior, fresh)
    body = render_ledger(final)

    summary = RunSummary(
        created=ledger is None,
        total=len(final),
        open=sum(1 for p in final if p.state == "open"),
        refuted_dropped=refuted_dropped,
    )
    if dry_run:
        return summary, body
    if ledger is not None:
        await io.update_ledger(ledger.node_id, body)
    else:
        if not board.repo_id:
            raise RuntimeError("cannot create Graph Review issue: repository id missing")
        await io.create_ledger(board.repo_id, body)
    return summary, body


# --- CLI ---------------------------------------------------------------------


def _build_io(workflow_path: Path) -> tuple[GraphReviewGitHub, GitHubTracker]:
    cfg = Config(load_workflow(workflow_path), workflow_path.parent).tracker()
    if cfg.kind != "github" or not cfg.repo:
        raise SystemExit("graph-review requires a github tracker with a repo in the workflow config")
    if not cfg.api_key:
        raise SystemExit("graph-review: tracker api_key unresolved (set $GITHUB_TOKEN)")
    tracker = GitHubTracker(cfg)
    return GraphReviewGitHub(tracker, cfg.repo), tracker


def main(argv: list[str] | None = None) -> int:
    import asyncio

    parser = argparse.ArgumentParser(
        prog="graph-review",
        description="Read-only graph-review analyzer: write evidence-cited "
        "proposals to the rolling Graph Review issue (Phase 1).",
    )
    parser.add_argument("--workflow", default="WORKFLOW.md", metavar="path",
                        help="path to the composed WORKFLOW.md (for tracker config)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the ledger body without writing to GitHub")
    parser.add_argument("--refute-command", default="claude -p",
                        help="command for the skeptic refute pass (default: 'claude -p')")
    args = parser.parse_args(argv)

    wf = Path(args.workflow)
    if not wf.is_file():
        log("graph-review startup failed", error=f"workflow file not found: {wf}")
        return 2

    io, tracker = _build_io(wf)
    refuter = claude_refuter(args.refute_command)

    async def _run() -> int:
        try:
            summary, body = await run_analysis(io, refuter, dry_run=args.dry_run)
        finally:
            await tracker.aclose()
        if args.dry_run:
            print(body)
        log(
            "graph-review complete",
            action="created" if summary.created else "updated",
            proposals=summary.total,
            open=summary.open,
            refuted_dropped=summary.refuted_dropped,
            dry_run=args.dry_run,
        )
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
