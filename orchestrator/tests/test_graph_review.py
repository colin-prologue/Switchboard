"""Tests for the graph-review analyzer (issue #37, Phase 1).

Maps 1:1 to the acceptance criteria. Detection tests are pure (hand-built
`Board`); idempotency / find-or-create / no-mutation tests drive the real
`GraphReviewGitHub` over httpx.MockTransport (no network), mirroring
test_tracker.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from orchestrator.graph_review import (
    _BOARD_QUERY,
    LEDGER_MARKER,
    Board,
    BoardIssue,
    Comment,
    GraphReviewGitHub,
    MergedPR,
    MilestoneRef,
    Proposal,
    RefuteVerdict,
    apply_refutation,
    detect_all,
    detect_edges,
    detect_merges,
    detect_milestones,
    detect_promotable,
    detect_resequence,
    detect_splits,
    detect_stale_assumptions,
    find_ledger,
    never_refute,
    parse_ledger,
    reconcile,
    render_ledger,
    run_analysis,
)
from orchestrator.tracker import GitHubTracker
from orchestrator.types import TrackerConfig


# --- builders ----------------------------------------------------------------


def bi(
    number,
    *,
    title="Some issue",
    body="",
    labels=None,
    milestone=None,
    blocked_by=None,
    blocker_states=None,
    comments=None,
) -> BoardIssue:
    return BoardIssue(
        number=number,
        node_id=f"I_{number}",
        title=title,
        body=body,
        state="open",
        labels=labels or [],
        milestone=milestone,
        blocked_by=blocked_by or [],
        blocker_states=blocker_states or {},
        comments=comments or [],
        url=f"https://gh/x/issues/{number}",
    )


def ms(title, due=None) -> MilestoneRef:
    return MilestoneRef(title=title, due_on=due)


DT = lambda d: datetime(2026, d, 1, tzinfo=timezone.utc)  # noqa: E731


# --- AC-4 / AC-6: categories -------------------------------------------------


def test_edge_prose_dependency_without_native_edge():
    board = Board(issues=[bi(31, body="This depends on #16 to land first."), bi(16)], merged_prs=[])
    props = detect_edges(board)
    assert [p.key for p in props] == ["edge:16,31"]
    assert props[0].category == "edge"
    assert "#31" in props[0].evidence[0]


def test_edge_soft_see_also_reference_is_not_flagged():
    board = Board(issues=[bi(31, body="See also #16 for background."), bi(16)], merged_prs=[])
    assert detect_edges(board) == []


def test_edge_suppressed_when_native_blockedby_exists_either_direction():
    # AC-6: #16 already has a blockedBy edge to #15 -> no missing-edge proposal.
    board = Board(
        issues=[
            bi(16, body="blocked by #15", blocked_by=[15], blocker_states={15: "open"}),
            bi(15),
        ],
        merged_prs=[],
    )
    assert detect_edges(board) == []
    # reverse direction: prose in #15 says it depends on #16, native edge lives
    # on #16 -> still suppressed for the pair.
    board2 = Board(issues=[bi(16, blocked_by=[15]), bi(15, body="depends on #16")], merged_prs=[])
    assert detect_edges(board2) == []


def test_milestone_missing_when_blocker_is_milestoned():
    board = Board(
        issues=[bi(29, blocked_by=[16]), bi(16, milestone=ms("v0.2"))],
        merged_prs=[],
    )
    props = detect_milestones(board)
    assert [p.key for p in props] == ["milestone:29"]


def test_resequence_blocker_scheduled_after_dependent():
    board = Board(
        issues=[
            bi(29, milestone=ms("v0.2", DT(6)), blocked_by=[16]),
            bi(16, milestone=ms("v0.3", DT(9))),
        ],
        merged_prs=[],
    )
    props = detect_resequence(board)
    assert [p.key for p in props] == ["resequence:16,29"]


def test_merge_candidate_on_cross_reference_and_title_overlap():
    board = Board(
        issues=[
            bi(31, title="graph review analyzer proposals", body="dup of #35"),
            bi(35, title="graph review analyzer proposals ledger", body="see #31"),
        ],
        merged_prs=[],
    )
    props = detect_merges(board)
    assert [p.key for p in props] == ["merge:31,35"]


def test_split_candidate_on_many_checkboxes():
    body = "\n".join(f"- [ ] deliverable {i}" for i in range(6))
    props = detect_splits(Board(issues=[bi(35, body=body)], merged_prs=[]))
    assert [p.key for p in props] == ["split:35"]


def test_stale_assumption_when_merged_pr_supersedes_issue():
    board = Board(
        issues=[bi(35, body="Assumption: the API is stable.")],
        merged_prs=[MergedPR(number=40, title="rework", body="supersedes #35",
                             merge_sha="abc123def456", merged_at=DT(7), closes=[])],
    )
    props = detect_stale_assumptions(board)
    assert [p.key for p in props] == ["stale-assumption:35"]
    assert "abc123def4" in props[0].evidence[0]


def test_promotable_when_all_blockers_closed():
    board = Board(
        issues=[bi(20, blocked_by=[10, 11], blocker_states={10: "closed", 11: "closed"})],
        merged_prs=[],
    )
    assert [p.key for p in detect_promotable(board)] == ["promotable:20"]


def test_promotable_not_raised_when_a_blocker_still_open():
    board = Board(
        issues=[bi(20, blocked_by=[10, 11], blocker_states={10: "closed", 11: "open"})],
        merged_prs=[],
    )
    assert detect_promotable(board) == []


# --- AC-2: proposal record shape --------------------------------------------


def test_proposal_key_is_category_colon_sorted_issue_list():
    board = Board(issues=[bi(31, body="requires #16"), bi(16)], merged_prs=[])
    p = detect_edges(board)[0]
    assert p.key == "edge:16,31"  # sorted, not "31,16"
    assert p.state == "open"
    assert p.action and p.evidence  # suggested action + cited evidence present


# --- AC-5: refute sub-check --------------------------------------------------


def test_refuter_drops_structural_but_never_runs_on_mechanical():
    calls = []

    def refute_everything(p, _board):
        calls.append(p.key)
        return RefuteVerdict(refuted=True, reason="disproved")

    proposals = [
        Proposal(key="edge:16,31", category="edge", action="a"),
        Proposal(key="merge:31,35", category="merge", action="a"),
    ]
    kept = apply_refutation(proposals, Board(issues=[], merged_prs=[]), refute_everything)
    assert [p.key for p in kept] == ["edge:16,31"]      # mechanical survives
    assert calls == ["merge:31,35"]                     # refuter only saw the merge


def test_refuter_keeps_structural_it_cannot_disprove():
    proposals = [Proposal(key="merge:31,35", category="merge", action="a")]
    kept = apply_refutation(proposals, Board(issues=[], merged_prs=[]), never_refute)
    assert [p.key for p in kept] == ["merge:31,35"]


def test_refuter_error_drops_structural_conservatively():
    def boom(_p, _b):
        raise RuntimeError("skeptic unavailable")

    proposals = [Proposal(key="merge:31,35", category="merge", action="a")]
    assert apply_refutation(proposals, Board(issues=[], merged_prs=[]), boom) == []


# --- AC-3: ledger render/parse + never re-raise disposed keys ----------------


def test_render_parse_round_trip_preserves_state():
    props = [
        Proposal(key="edge:16,31", category="edge", action="add edge", evidence=["#31: x"]),
        Proposal(key="merge:31,35", category="merge", action="merge", evidence=["a", "b"],
                 state="dismissed"),
    ]
    body = render_ledger(props)
    parsed = {p.key: p for p in parse_ledger(body)}
    assert parsed["edge:16,31"].state == "open"
    assert parsed["merge:31,35"].state == "dismissed"
    assert parsed["edge:16,31"].action == "add edge"
    assert parsed["merge:31,35"].evidence == ["a", "b"]


def test_reconcile_never_reraises_dismissed_or_accepted_key():
    prior = [
        Proposal(key="merge:31,35", category="merge", action="m", state="dismissed"),
        Proposal(key="edge:16,31", category="edge", action="e", state="accepted"),
    ]
    # detector regenerates both keys plus a new one.
    fresh = [
        Proposal(key="merge:31,35", category="merge", action="m"),
        Proposal(key="edge:16,31", category="edge", action="e"),
        Proposal(key="promotable:20", category="promotable", action="p"),
    ]
    result = {p.key: p for p in reconcile(prior, fresh)}
    assert result["merge:31,35"].state == "dismissed"   # preserved, not re-opened
    assert result["edge:16,31"].state == "accepted"
    assert result["promotable:20"].state == "open"      # genuinely new -> raised
    assert sum(1 for k in result) == 3                  # exactly one of each key


def test_reconcile_drops_resolved_open_proposal():
    prior = [Proposal(key="promotable:20", category="promotable", action="p", state="open")]
    assert reconcile(prior, fresh=[]) == []


# --- I/O boundary: httpx.MockTransport ---------------------------------------


def _cfg() -> TrackerConfig:
    return TrackerConfig(
        kind="github", repo="acme/widgets", endpoint="https://api.github.com/graphql",
        api_key="t", required_labels=[], active_states=["todo"], terminal_states=["closed"],
    )


def _board_response(issue_nodes, pr_nodes=(), repo_id="R_1"):
    return {
        "repository": {
            "id": repo_id,
            "issues": {"nodes": list(issue_nodes), "pageInfo": {"hasNextPage": False}},
            "pullRequests": {"nodes": list(pr_nodes)},
        }
    }


def _issue_node(number, *, title="t", body="", labels=(), milestone=None,
                blocked_by=(), comments=()):
    return {
        "id": f"I_{number}", "number": number, "title": title, "body": body,
        "url": f"https://gh/x/issues/{number}", "state": "OPEN",
        "labels": {"nodes": [{"name": n} for n in labels]},
        "milestone": milestone,
        "blockedBy": {"nodes": list(blocked_by)},
        "comments": {"nodes": list(comments)},
    }


class Recorder:
    def __init__(self, board_data):
        self.board_data = board_data
        self.mutations: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        query = body["query"]
        if "createIssue" in query or "updateIssue" in query:
            self.mutations.append(body)
            return httpx.Response(200, json={"data": {"ok": {"issue": {"id": "I_new", "number": 99}}}})
        return httpx.Response(200, json={"data": self.board_data})


def _io(recorder: Recorder) -> tuple[GraphReviewGitHub, GitHubTracker]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))
    tracker = GitHubTracker(_cfg(), client=client)
    return GraphReviewGitHub(tracker, "acme/widgets"), tracker


# --- AC-6: query uses blockedBy, never trackedIssues/trackedInIssues ---------


def test_board_query_reads_blockedby_not_tracked_issues():
    assert "blockedBy" in _BOARD_QUERY
    assert "trackedIssues" not in _BOARD_QUERY
    assert "trackedInIssues" not in _BOARD_QUERY


# --- AC-1: idempotent single ledger ------------------------------------------


@pytest.mark.asyncio
async def test_creates_ledger_when_absent_then_updates_on_rerun():
    # First run: no ledger issue exists -> exactly one create.
    board = _board_response([_issue_node(31, body="depends on #16"), _issue_node(16)])
    rec = Recorder(board)
    io, tracker = _io(rec)
    summary, body = await run_analysis(io, never_refute)
    assert summary.created is True
    assert len(rec.mutations) == 1
    assert "createIssue" in rec.mutations[0]["query"]
    created_body = rec.mutations[0]["variables"]["body"]
    assert LEDGER_MARKER in created_body

    # Second run: the ledger now exists on the board -> update in place, no create.
    ledger_node = _issue_node(99, title="Graph Review", body=created_body)
    board2 = _board_response([_issue_node(31, body="depends on #16"), _issue_node(16), ledger_node])
    rec2 = Recorder(board2)
    io2, _ = _io(rec2)
    summary2, _ = await run_analysis(io2, never_refute)
    assert summary2.created is False
    assert len(rec2.mutations) == 1
    assert "updateIssue" in rec2.mutations[0]["query"]
    assert rec2.mutations[0]["variables"]["id"] == "I_99"
    await tracker.aclose()


def test_find_ledger_by_marker_and_by_title():
    marked = bi(5, body=f"junk\n{LEDGER_MARKER}\nmore")
    assert find_ledger(Board(issues=[bi(1), marked], merged_prs=[])) is marked
    titled = bi(7, title="Graph Review")
    assert find_ledger(Board(issues=[bi(1), titled], merged_prs=[])) is titled
    assert find_ledger(Board(issues=[bi(1)], merged_prs=[])) is None


# --- AC-7: no mutation of any other issue ------------------------------------


@pytest.mark.asyncio
async def test_run_only_mutation_is_the_ledger_no_other_writes():
    ledger_node = _issue_node(99, title="Graph Review", body=LEDGER_MARKER)
    board = _board_response(
        [_issue_node(31, body="depends on #16"), _issue_node(16), ledger_node]
    )
    rec = Recorder(board)
    io, tracker = _io(rec)
    await run_analysis(io, never_refute)
    # Exactly one mutation, and it targets the ledger node (updateIssue on I_99).
    assert len(rec.mutations) == 1
    m = rec.mutations[0]
    assert "updateIssue" in m["query"]
    assert m["variables"]["id"] == "I_99"
    # No addLabels / addComment / edge mutations anywhere.
    assert all("addLabels" not in x["query"] and "addComment" not in x["query"]
               for x in rec.mutations)
    await tracker.aclose()


@pytest.mark.asyncio
async def test_dry_run_writes_nothing():
    board = _board_response([_issue_node(31, body="depends on #16"), _issue_node(16)])
    rec = Recorder(board)
    io, tracker = _io(rec)
    summary, body = await run_analysis(io, never_refute, dry_run=True)
    assert rec.mutations == []
    assert LEDGER_MARKER in body
    await tracker.aclose()


# --- AC-4 integration: full detector sweep produces the expected keys --------


def test_detect_all_collects_every_category():
    board = Board(
        issues=[
            bi(31, title="alpha widget sync", body="depends on #16\ndup of #35"),
            bi(35, title="alpha widget sync ledger", body="see #31\n"
               + "\n".join(f"- [ ] task {i}" for i in range(6))),
            bi(16, milestone=ms("v0.2", DT(6))),
            bi(29, milestone=ms("v0.1", DT(3)), blocked_by=[16]),   # blocker later -> resequence
            bi(40, blocked_by=[16]),                                 # missing milestone
            bi(20, blocked_by=[10], blocker_states={10: "closed"}),  # promotable
        ],
        merged_prs=[MergedPR(number=50, title="x", body="supersedes #31",
                             merge_sha="deadbeef00", merged_at=DT(7), closes=[])],
    )
    keys = {p.key for p in detect_all(board)}
    assert "edge:16,31" in keys
    assert "milestone:40" in keys
    assert "resequence:16,29" in keys
    assert "merge:31,35" in keys
    assert "split:35" in keys
    assert "stale-assumption:31" in keys
    assert "promotable:20" in keys


# --- Codex PR #41 P1: ledger discovery must survive >100 open issues ----------


def _minimal_node(number, *, title="t", body=""):
    return {"id": f"I_{number}", "number": number, "title": title, "body": body}


class PagedRecorder(Recorder):
    """Serves the board query (truncated) plus sequential ledger-scan pages."""

    def __init__(self, board_data, scan_pages):
        super().__init__(board_data)
        self.scan_pages = list(scan_pages)  # [(nodes, has_next, end_cursor), ...]
        self.scan_calls: list[str | None] = []  # `after` cursor per scan call

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if "$prCount" in body["query"] or "createIssue" in body["query"] \
                or "updateIssue" in body["query"]:
            return super().handler(request)
        # ledger-scan query
        self.scan_calls.append(body["variables"].get("after"))
        nodes, has_next, cursor = self.scan_pages[len(self.scan_calls) - 1]
        return httpx.Response(200, json={"data": {"repository": {"issues": {
            "nodes": nodes,
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        }}}})


def _truncated_board(issue_nodes, end_cursor="CUR_1"):
    resp = _board_response(issue_nodes)
    resp["repository"]["issues"]["pageInfo"] = {
        "hasNextPage": True, "endCursor": end_cursor}
    return resp


@pytest.mark.asyncio
async def test_ledger_beyond_first_page_is_updated_not_duplicated():
    # Board is truncated at the first page and the ledger lives on page 2:
    # discovery must page on and UPDATE it — a duplicate ledger per run was
    # the Codex P1 failure mode.
    ledger_body = f"{LEDGER_MARKER}\n(prior)"
    board = _truncated_board([_issue_node(31, body="depends on #16"), _issue_node(16)])
    rec = PagedRecorder(board, scan_pages=[
        ([_minimal_node(200), _minimal_node(201)], True, "CUR_2"),
        ([_minimal_node(300, title="Graph Review", body=ledger_body)], False, None),
    ])
    io, tracker = _io(rec)

    summary, _ = await run_analysis(io, never_refute)

    assert summary.created is False
    assert len(rec.mutations) == 1
    assert "updateIssue" in rec.mutations[0]["query"]
    assert rec.mutations[0]["variables"]["id"] == "I_300"
    # scan resumed FROM the board's cursor, then followed page cursors
    assert rec.scan_calls == ["CUR_1", "CUR_2"]
    await tracker.aclose()


@pytest.mark.asyncio
async def test_truncated_board_with_no_ledger_anywhere_creates_once():
    board = _truncated_board([_issue_node(31)])
    rec = PagedRecorder(board, scan_pages=[([_minimal_node(200)], False, None)])
    io, tracker = _io(rec)

    summary, _ = await run_analysis(io, never_refute)

    assert summary.created is True
    assert len(rec.mutations) == 1
    assert "createIssue" in rec.mutations[0]["query"]
    assert rec.scan_calls == ["CUR_1"]  # exhausted the scan before creating
    await tracker.aclose()


@pytest.mark.asyncio
async def test_untruncated_board_never_issues_scan_query():
    # The common case (<100 open issues) must stay a single board query.
    board = _board_response([_issue_node(31)])
    rec = PagedRecorder(board, scan_pages=[])
    io, tracker = _io(rec)

    await run_analysis(io, never_refute)

    assert rec.scan_calls == []
    await tracker.aclose()
