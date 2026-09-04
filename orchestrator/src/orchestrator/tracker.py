"""GitHub Issues tracker adapter.

implements: core §11 (Issue Tracker Integration Contract)
overridden by: spec/SPEC.md §2 (Tracker binding — Linear -> GitHub Issues)

Binding summary (SPEC.md §2):
- tracker.kind == "github"; one process == one repo (tracker.repo "owner/name").
- Workflow STATE has no first-class GitHub equivalent (issues are only
  open/closed), so it is modeled as `status:<name>` labels. A `status:in-progress`
  label normalizes to state "in progress" ("-" -> " ").
- Issue closed -> terminal state "closed"; status:* labels are otherwise
  non-terminal and only meaningful while the issue is open.
- `blocked_by` is read from GitHub's native issue-dependencies GraphQL
  connection: `blockedBy(first:N){ nodes { id number state } }`.
- `issue.identifier` is the issue NUMBER as a string (not a node id).

Query construction is kept isolated as module-level GraphQL string constants
(core §11.2 note: "Keep query construction isolated and test the exact query
fields/types REQUIRED by this specification").
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Any

import httpx

from orchestrator.auth import AppInstallationTokenProvider, StaticTokenProvider
from orchestrator.log import log
from orchestrator.transitions import load_precedence
from orchestrator.types import (
    BlockerRef,
    CommentReaction,
    Issue,
    IssueComment,
    ReviewThread,
    ReviewThreadComment,
    TrackerConfig,
    TrackerError,
)

# --- GraphQL query/mutation constants (core §11.2) --------------------------

_ISSUE_FIELDS = """
    id
    number
    title
    body
    url
    state
    createdAt
    updatedAt
    labels(first: 50) {
      nodes { name }
    }
    blockedBy(first: 20) {
      nodes { id number state }
    }
"""

_ISSUE_FIELDS_NO_BLOCKERS = """
    id
    number
    title
    body
    url
    state
    createdAt
    updatedAt
    labels(first: 50) {
      nodes { name }
    }
"""

# fetch_candidate_issues / fetch_issues_by_states: paginated repository issues.
CANDIDATE_ISSUES_QUERY = f"""
query($owner: String!, $name: String!, $states: [IssueState!]!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    issues(first: 50, after: $after, states: $states, orderBy: {{field: CREATED_AT, direction: ASC}}) {{
      nodes {{
        {_ISSUE_FIELDS}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

# fetch_issues_by_states terminal cleanup: closed issues, no blockedBy needed.
CLOSED_ISSUES_QUERY = f"""
query($owner: String!, $name: String!, $after: String) {{
  repository(owner: $owner, name: $name) {{
    issues(first: 50, after: $after, states: [CLOSED], orderBy: {{field: CREATED_AT, direction: ASC}}) {{
      nodes {{
        {_ISSUE_FIELDS_NO_BLOCKERS}
      }}
      pageInfo {{ hasNextPage endCursor }}
    }}
  }}
}}
"""

# fetch_issue_comments: verdict-comment fold-signal detection (issue #51 part a).
# GitHub issue comments have no reply threading, so every comment is top-level
# and the connection is flat. The login field on a reaction is `user`, NOT
# `author` (only Comment-ish types carry `author`) — asking for `author` here is
# a schema error, which is why this query's exact shape is pinned by a test.
# Reactions are read as full nodes rather than `reactionGroups` because the
# per-reaction node id is the dedupe key and the reacting login must be matched
# against the operator allowlist — presence alone is not enough.
ISSUE_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      comments(first: 50, after: $after) {
        nodes {
          id
          body
          createdAt
          author { login }
          reactions(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { id content createdAt user { login } }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

# fetch_issue_states_by_ids: reconciliation by node id.
ISSUES_BY_IDS_QUERY = f"""
query($ids: [ID!]!) {{
  nodes(ids: $ids) {{
    ... on Issue {{
      {_ISSUE_FIELDS}
    }}
  }}
}}
"""

# add_issue_comment: owned Switchboard extension (parking notification).
ADD_COMMENT_MUTATION = """
mutation($subjectId: ID!, $body: String!) {
  addComment(input: {subjectId: $subjectId, body: $body}) {
    commentEdge { node { id } }
  }
}
"""


# update_issue_body: the fold apply step's ONE net-new write (issue #126 part b).
# GitHub has no conditional update, so this is the write half of a
# read-compare-write, never a compare-and-swap; the TOCTOU window is an accepted
# residual and verify-after-write is the check that survives it.
UPDATE_ISSUE_BODY_MUTATION = """
mutation($id: ID!, $body: String!) {
  updateIssue(input: {id: $id, body: $body}) {
    issue { id }
  }
}
"""


COMMENT_REACTIONS_PAGE_QUERY = """
query($commentId: ID!, $cursor: String) {
  node(id: $commentId) {
    ... on IssueComment {
      reactions(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id content createdAt user { login } }
      }
    }
  }
}
"""

OPEN_PRS_FOR_BRANCH_QUERY = """
query($owner: String!, $name: String!, $headRef: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $headRef, states: [OPEN], first: 5) {
      nodes {
        id
        number
        headRefOid
        closingIssuesReferences(first: 50) {
          pageInfo { hasNextPage endCursor }
          nodes { number }
        }
      }
    }
  }
}
"""

CLOSING_REFS_PAGE_QUERY = """
query($owner: String!, $name: String!, $prNumber: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $prNumber) {
      closingIssuesReferences(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { number }
      }
    }
  }
}
"""

# fetch_pr_comments: round-marker reader (issue #43). `repository.issue(number:)`
# is NULL for a pull-request number, so ISSUE_COMMENTS_QUERY cannot serve this —
# hence a sibling query on `pullRequest`. No reactions: no marker reader wants
# them, and selecting them would cost a nested page per comment for nothing.
PR_COMMENTS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 50, after: $after) {
        nodes { id body createdAt author { login } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

# fetch_pr_review_threads: the review-response trigger's read surface (issue
# #43). The selection is exactly the `needs_response` fixture set — `isResolved`
# plus per-comment author login and `createdAt`. Bodies are NOT selected: the
# predicate never reads text, and the responding session fetches the threads
# itself. Verified live against PR #132 (2026-08-09): a Bot author's `login` is
# the BARE login (`chatgpt-codex-connector`, `switchboard-agent`) with
# `__typename: Bot` — never the `<login>[bot]` form git uses.
PR_REVIEW_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $after) {
        nodes {
          id
          isResolved
          comments(first: 50) {
            nodes { id createdAt author { login } }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

REVIEW_THREAD_COMMENTS_PAGE_QUERY = """
query($threadId: ID!, $cursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      comments(first: 50, after: $cursor) {
        nodes { id createdAt author { login } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

# issue #165: the ops log. Identified by TITLE, not by a label — a label would
# have to be resolved (and created when absent) before the issue can be made,
# and the whole point of this path is that it works with no prior setup on a
# repo the operator has walked away from. An issue with no `status:*` label
# normalizes to state "none" (`normalize_status_state`), which is in no stance's
# active set, so the ops log is never dispatched to an agent.
OPS_ISSUE_TITLE = "Switchboard ops log"

# issue #192: the operator inbox digest's own surface. Same find-or-create-by-
# TITLE mechanism and the same no-`status:*`-label property as the ops log, for
# the same two reasons (works with no prior setup; the dispatcher can never
# claim it). A SEPARATE issue rather than a section of the ops log because the
# two have incompatible write modes: latch notices are append-only comments by
# design (a stopped system's only signal must not be overwritable), and the
# digest is a whole-body replacement.
DIGEST_ISSUE_TITLE = "Switchboard operator inbox"

# fetch_open_prs_repo_wide: the digest's PR section (issue #192). `fetch_open_prs`
# is head-ref-scoped (built for handoff evidence) and so structurally cannot
# answer "what PRs are open in this repo". Reads were never the §11.5
# restriction; only writes are.
OPEN_PRS_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: [OPEN], first: 50, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
      nodes { number title url isDraft headRefName createdAt updatedAt }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

REPOSITORY_ID_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) { id }
}
"""

CREATE_ISSUE_MUTATION = """
mutation($repositoryId: ID!, $title: String!, $body: String!) {
  createIssue(input: {repositoryId: $repositoryId, title: $title, body: $body}) {
    issue { id number }
  }
}
"""

LABEL_ID_QUERY = """
query($owner: String!, $name: String!, $label: String!) {
  repository(owner: $owner, name: $name) {
    label(name: $label) { id }
  }
}
"""

ADD_LABELS_MUTATION = """
mutation($labelableId: ID!, $labelIds: [ID!]!) {
  addLabelsToLabelable(input: {labelableId: $labelableId, labelIds: $labelIds}) {
    clientMutationId
  }
}
"""

REMOVE_LABELS_MUTATION = """
mutation($labelableId: ID!, $labelIds: [ID!]!) {
  removeLabelsFromLabelable(input: {labelableId: $labelableId, labelIds: $labelIds}) {
    clientMutationId
  }
}
"""


STATUS_PREFIX = "status:"

# `status:*` labels that are MARKERS, not a claim about the issue's current
# workflow state. `status:parked` is durable by design (a park must survive a
# restart) and legitimately coexists with a live status label, so anything
# counting "how many labels claim to be the state" must exclude it.
DURABLE_STATUS_MARKERS = frozenset({"status:parked"})


def status_labels(labels: list[str]) -> list[str]:
    """Every `status:*` label on the issue, sorted (markers included)."""
    return sorted(l for l in labels if l.startswith(STATUS_PREFIX))


def live_status_labels(labels: list[str]) -> list[str]:
    """The `status:*` labels claiming to BE the issue's current state, sorted.

    Same list as `status_labels` minus the durable markers. The two live
    together because the difference between them is the whole subtlety: the
    normalization diagnostic below asks "is the state I derived arbitrary?"
    (markers count — `parked` + `triage` derives `parked`), while the
    board-state check asks "do two labels both claim to be the state?"
    (markers do not count). One vocabulary, two questions.
    """
    return sorted(l for l in status_labels(labels) if l not in DURABLE_STATUS_MARKERS)


@lru_cache(maxsize=1)
def status_precedence() -> tuple[str, ...]:
    """The committed state precedence, highest first (`workflow/transitions.yml`).

    Cached: `normalize_status_state` runs once per issue per poll, and the table
    is a committed file that only changes with a deploy. Call
    `status_precedence.cache_clear()` if a test needs to re-read it.
    """
    return load_precedence()


def normalize_status_state(
    labels: list[str], *, closed: bool, precedence: tuple[str, ...] | None = None
) -> str:
    """Derive the workflow state from an issue's labels (SPEC.md §2).

    The single source of truth for status:* -> state mapping so test fakes can
    assert fidelity against it instead of hard-coding states: closed issues are
    terminal "closed"; otherwise "status:" is stripped, "-" -> " ", and the
    highest-ranked state under `precedence` wins; no status label -> "none".

    `precedence` is the committed list in `workflow/transitions.yml`, EXPLICIT
    rather than alphabetical (issue #167). One status label per issue is still
    the contract for stage labels, but `status:parked` is an overlay applied
    alongside the stage a ticket resumes into, so a second label is a designed
    state and the winner has to be a stated decision. Under the old
    `sorted(...)[0]` a parked issue carrying the claim label derived `in
    progress` — an ACTIVE state — and only a separate best-effort strip and an
    explicit `PARK_LABEL` check kept it out of dispatch. A state absent from the
    list ranks below every listed one (ties alphabetical), so an undefined
    `status:*` label can never out-vote a real state yet still derives
    deterministically for the board-state check.

    Labels are assumed already normalized (stripped, lower-cased) as
    `_normalize_issue` produces them. Pure given `precedence`; the default is
    read from the committed table. The multi-label diagnostic lives in
    `_normalize_issue`, which has the issue number to log.
    """
    if closed:
        return "closed"
    states = sorted(
        l[len(STATUS_PREFIX) :].replace("-", " ")
        for l in labels
        if l.startswith(STATUS_PREFIX)
    )
    if not states:
        return "none"
    ranks = status_precedence() if precedence is None else precedence
    order = {state: i for i, state in enumerate(ranks)}
    unranked = len(order)
    return min(states, key=lambda s: (order.get(s, unranked), s))


class GitHubTracker:
    """Tracker adapter bound to GitHub Issues (SPEC.md §2).

    implements: core §11.1 (required operations) / overridden by: SPEC.md §2
    """

    def __init__(
        self,
        cfg: TrackerConfig,
        client: httpx.AsyncClient | None = None,
        creds: StaticTokenProvider | AppInstallationTokenProvider | None = None,
    ) -> None:
        self._cfg = cfg
        self._owned_client = client is None
        # core §11.2: network timeout 30000 ms.
        self._client = client or httpx.AsyncClient(timeout=30.0)
        # issue #10: the token comes from a provider (App installation token
        # preferred, minted/re-minted at runtime). Absent an explicit provider,
        # fall back to the statically-resolved cfg.api_key so the tracker stays
        # usable standalone/in tests without wiring the scheduler.
        self._creds = creds or StaticTokenProvider(cfg.api_key)
        # label name -> node id; labels are immutable ids per repo, safe to cache.
        self._label_id_cache: dict[str, str] = {}

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    # --- required operations (core §11.1) ------------------------------------

    async def fetch_open_issues(self) -> list[Issue]:
        """EVERY open issue in the repo, normalized, unfiltered (issue #52).

        `fetch_candidate_issues` is this query plus the active-state filter, and
        the filter is exactly what hides the states the board-state check looks
        for — an undefined `status:*` label normalizes to a state no
        `active_states` list contains, so a filtered fetch can never see one.
        Splitting the two lets a caller pay for one query and get both answers;
        the sanity check adds no API calls (issue #52's cost assumption).
        """
        owner, name = self._split_repo()
        raw_issues = await self._paginate(
            CANDIDATE_ISSUES_QUERY, {"owner": owner, "name": name, "states": ["OPEN"]}
        )
        return [self._normalize_issue(raw) for raw in raw_issues]

    def select_candidates(self, issues: list[Issue]) -> list[Issue]:
        """The dispatchable subset of already-fetched open issues.

        The active-state filter lives here, in ONE place, so a caller that
        fetched the unfiltered set does not re-implement eligibility.
        """
        active = set(self._cfg.active_states)
        return [issue for issue in issues if issue.state in active]

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Open issues in the repo, filtered to `cfg.active_states` post-normalization.

        implements: core §11.1(1) / overridden by: SPEC.md §2 (repo instead of
        project_slug; state from status:* labels instead of first-class state)
        """
        return self.select_candidates(await self.fetch_open_issues())

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        """Startup terminal cleanup lookup.

        implements: core §11.1(2) / overridden by: SPEC.md §2

        GitHub has no server-side form for arbitrary named states — only
        open/closed. In this binding, "terminal" == GitHub CLOSED. If any of
        `state_names` is the terminal "closed" state, this queries CLOSED
        issues; any other requested state names have no GitHub-side query and
        are simply not represented (they cannot be closed-backed). An empty
        request list makes zero API calls (core §17.3).
        """
        if not state_names:
            return []
        if "closed" not in state_names:
            return []
        owner, name = self._split_repo()
        raw_issues = await self._paginate(CLOSED_ISSUES_QUERY, {"owner": owner, "name": name})
        return [self._normalize_issue(raw) for raw in raw_issues]

    async def fetch_open_issues_by_status(self, status_names: list[str]) -> list[Issue]:
        """OPEN issues whose normalized state is one of `status_names`.

        Owned extension (issue #51 part a). `fetch_candidate_issues` filters to
        `cfg.active_states`, and `fetch_issues_by_states` only has a GitHub-side
        form for CLOSED — so neither can see a *gate* state like `drafting` or
        `decision`, which is exactly the set the fold poller must watch. This
        reuses CANDIDATE_ISSUES_QUERY (already parameterized on `states`) with
        the same client-side `issue.state` filter `fetch_candidate_issues`
        applies, against an explicit list instead of the active set. Reading
        gate states here has NO dispatch side-effect: dispatch eligibility still
        keys off `active_states`, which is untouched.

        An empty request list makes zero API calls (core §17.3).
        """
        wanted = {s.strip().lower() for s in status_names if s and s.strip()}
        if not wanted:
            return []
        owner, name = self._split_repo()
        raw_issues = await self._paginate(
            CANDIDATE_ISSUES_QUERY, {"owner": owner, "name": name, "states": ["OPEN"]}
        )
        issues = [self._normalize_issue(raw) for raw in raw_issues]
        return [issue for issue in issues if issue.state in wanted]

    async def fetch_issue_comments(self, issue_number: str | int) -> list[IssueComment]:
        """Top-level comments on one issue, with their reactions (issue #51).

        Read-only. Ordered oldest-first as GitHub returns them, which is the
        order the fold binding rule ("the most recent verdict comment preceding
        this one") depends on.
        """
        owner, name = self._split_repo()
        try:
            number = int(issue_number)
        except (TypeError, ValueError) as exc:
            raise TrackerError(
                "github_unknown_payload",
                f"issue identifier {issue_number!r} is not a number",
            ) from exc

        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._request(
                ISSUE_COMMENTS_QUERY,
                {"owner": owner, "name": name, "number": number, "after": after},
            )
            issue = (data.get("repository") or {}).get("issue")
            if issue is None:
                raise TrackerError(
                    "github_unknown_payload",
                    f"issue #{number} not found in repository payload",
                )
            conn = issue.get("comments")
            if not isinstance(conn, dict):
                raise TrackerError(
                    "github_unknown_payload", "missing 'issue.comments' connection"
                )
            page_nodes = conn.get("nodes")
            page_info = conn.get("pageInfo")
            if page_nodes is None or page_info is None:
                raise TrackerError(
                    "github_unknown_payload", "missing comments nodes/pageInfo"
                )
            nodes.extend(n for n in page_nodes if isinstance(n, dict))
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if after is None:
                raise TrackerError(
                    "github_missing_end_cursor", "hasNextPage true but endCursor missing"
                )
        # PR #129 review: a comment with >100 reactions truncates the nested
        # connection — paginate each such comment's reactions to completion so
        # an operator signal beyond the first page is never silently missed.
        for raw in nodes:
            rc = raw.get("reactions") or {}
            page = rc.get("pageInfo") or {}
            while page.get("hasNextPage"):
                more = await self._request(
                    COMMENT_REACTIONS_PAGE_QUERY,
                    {"commentId": raw.get("id"), "cursor": page.get("endCursor")},
                )
                rconn = ((more.get("node") or {}).get("reactions")) or {}
                (rc.setdefault("nodes", [])).extend(rconn.get("nodes") or [])
                page = rconn.get("pageInfo") or {}
        return [self._normalize_comment(node) for node in nodes]

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        """Active-run reconciliation by GraphQL node id.

        implements: core §11.1(3) / overridden by: SPEC.md §2 (variable type
        `[ID!]!` rather than core §11.2's Linear `[ID!]`)
        """
        if not issue_ids:
            return []
        data = await self._request(ISSUES_BY_IDS_QUERY, {"ids": issue_ids})
        nodes = data.get("nodes")
        if nodes is None:
            raise TrackerError("github_unknown_payload", "missing 'nodes' in response data")
        issues = []
        for node in nodes:
            # None = deleted/inaccessible; {} = node id that is not an Issue
            # (the `... on Issue` fragment matched nothing). Normalizing the
            # latter would KeyError, which upstream must never see as a
            # non-TrackerError surprise.
            if not isinstance(node, dict) or not node.get("id"):
                continue
            issues.append(self._normalize_issue(node))
        return issues

    # --- owned extension (SPEC.md §4: caps as diagnostic checkpoints) -------

    async def add_issue_comment(self, issue_id: str, body: str) -> None:
        """Post a parking-notification comment on an issue.

        overridden by: SPEC.md §4 owned extension (not part of core §11.1;
        core §11.5 keeps tracker writes out of the orchestrator in general,
        but the "park issue, notify once" behavior is an owned Switchboard
        extension that needs a single write path).
        """
        await self._request(ADD_COMMENT_MUTATION, {"subjectId": issue_id, "body": body})

    async def find_or_create_ops_issue(self) -> str:
        """Node id of this repo's ops log, creating it if absent (issue #165).

        A latched provider circuit is the one condition that stops ALL work and
        cannot clear itself — Switchboard is used in waves, so the operator may
        be a week from reading a terminal. This is the out-of-band channel that
        reaches them, and like `add_issue_comment` it is a sanctioned exception
        to the core §11.5 no-tracker-writes boundary.

        Find-or-create rather than configured: a notice that silently goes
        nowhere when a project skipped its setup is the failure mode this whole
        change exists to remove.
        """
        return await self._find_or_create_titled_issue(
            OPS_ISSUE_TITLE,
            "Switchboard posts operator-facing notices here — conditions "
            "that stop work and that only a human can clear.\n\n"
            "This issue carries no `status:*` label on purpose, so the "
            "orchestrator never dispatches it. Closing it is safe; the "
            "next notice opens a new one.",
        )

    async def find_or_create_digest_issue(self) -> str:
        """Node id of this repo's operator inbox, creating it if absent (#192).

        The ops log's mechanism, second instance — hence the shared helper
        rather than a second copy of the search-then-create walk. The seed body
        is deliberately NOT a digest: the first cycle replaces it wholesale, and
        a placeholder that looked like an empty inbox would be indistinguishable
        from a digest that ran and found nothing.
        """
        return await self._find_or_create_titled_issue(
            DIGEST_ISSUE_TITLE,
            "Switchboard rewrites this body on a schedule with what is waiting "
            "for you: issues sitting at a gate, open pull requests, parked "
            "issues and why, and the fail-review verdicts and cap-hit reports "
            "posted since the last digest.\n\n"
            "This issue carries no `status:*` label on purpose, so the "
            "orchestrator never dispatches it. Closing it is safe; the next "
            "digest opens a new one. Editing the body by hand is safe too — "
            "your edit stands until the next cycle re-renders over it.\n\n"
            "_No digest has been written yet._",
        )

    async def _find_or_create_titled_issue(self, title: str, body: str) -> str:
        """Node id of the OPEN issue with exactly `title`, created if absent.

        Identified by TITLE, not by a label — a label would have to be resolved
        (and created when absent) before the issue can be made, and both callers
        exist precisely so they work on a repo nobody prepared. Not idempotent
        under a lost create response; that residual is accepted (AgDR-046) and
        single-flighted by the caller.
        """
        owner, name = self._split_repo()
        raw_issues = await self._paginate(
            CANDIDATE_ISSUES_QUERY, {"owner": owner, "name": name, "states": ["OPEN"]}
        )
        for raw in raw_issues:
            issue = self._normalize_issue(raw)
            if issue.title.strip() == title:
                return issue.id
        repo = await self._request(REPOSITORY_ID_QUERY, {"owner": owner, "name": name})
        created = await self._request(
            CREATE_ISSUE_MUTATION,
            {
                "repositoryId": repo["repository"]["id"],
                "title": title,
                "body": body,
            },
        )
        return created["createIssue"]["issue"]["id"]

    async def update_issue_body(self, issue_id: str, body: str) -> None:
        """Replace an issue's body (issue #126: the fold apply step).

        The only net-new tracker surface part (b) needs — the single-issue body
        READ already exists (`fetch_issue_states_by_ids`, whose `_ISSUE_FIELDS`
        requests `body`). Like `add_issue_comment` and the label mutations, a
        sanctioned exception to the core §11.5 no-tracker-writes boundary: the
        operator's `/fold` is an explicit instruction to edit the body, and the
        caller (`fold_apply`) owns the read-compare-write around it.
        """
        await self._request(UPDATE_ISSUE_BODY_MUTATION, {"id": issue_id, "body": body})

    async def add_labels(self, issue_id: str, label_names: list[str]) -> None:
        """Apply labels to an issue (SPEC.md §4 owned extension: durable park).

        The `status:parked` marker must live in the tracker so parking survives
        a process restart. Like `add_issue_comment`, this is a sanctioned
        exception to the core §11.5 no-tracker-writes boundary. Label node ids
        are resolved by name (and cached) since GitHub's mutation takes ids.
        """
        label_ids = [await self._resolve_label_id(name) for name in label_names]
        await self._request(
            ADD_LABELS_MUTATION, {"labelableId": issue_id, "labelIds": label_ids}
        )

    async def remove_labels(self, issue_id: str, label_names: list[str]) -> None:
        """Remove labels from an issue (issue #14: claim-lifecycle visibility).

        The mirror of `add_labels` (`removeLabelsFromLabelable`). Used to keep
        the one-status-label contract when the orchestrator swaps a claim's
        status label (`status:todo` -> `status:in-progress` on dispatch, the
        reverse on claim release, and dropping `status:in-progress` at park).
        Like `add_labels`, a sanctioned exception to the core §11.5 no-writes
        boundary; label node ids are resolved by name (and cached).
        """
        label_ids = [await self._resolve_label_id(name) for name in label_names]
        await self._request(
            REMOVE_LABELS_MUTATION, {"labelableId": issue_id, "labelIds": label_ids}
        )

    async def fetch_open_prs(self, head_ref: str) -> list[dict]:
        """Open PRs whose head branch is `head_ref` (issue #61 handoff evidence).

        Returns `[{"number": int, "head_sha": str}]`. The handoff validator
        requires exactly one and matches its head oid against the evidence sha,
        proving the workspace commit was pushed and the PR is current.
        """
        owner, repo = self._split_repo()
        data = await self._request(
            OPEN_PRS_FOR_BRANCH_QUERY,
            {"owner": owner, "name": repo, "headRef": head_ref},
        )
        nodes = (
            ((data.get("repository") or {}).get("pullRequests") or {}).get("nodes")
            or []
        )
        out: list[dict] = []
        for node in nodes:
            if not isinstance(node, dict):
                continue
            number = node.get("number")
            head_sha = node.get("headRefOid")
            node_id = node.get("id")
            refs = node.get("closingIssuesReferences") or {}
            closes = [
                c["number"] for c in (refs.get("nodes") or [])
                if isinstance(c, dict) and isinstance(c.get("number"), int)
            ]
            # PR #115 review round 4: a PR closing more issues than one page
            # must not truncate — a missed reference would reject valid
            # linkage and burn worker retries. Paginate the connection.
            page = refs.get("pageInfo") or {}
            while page.get("hasNextPage") and isinstance(number, int):
                more = await self._request(
                    CLOSING_REFS_PAGE_QUERY,
                    {"owner": owner, "name": repo, "prNumber": number,
                     "cursor": page.get("endCursor")},
                )
                refs = (
                    (more.get("repository") or {}).get("pullRequest") or {}
                ).get("closingIssuesReferences") or {}
                closes.extend(
                    c["number"] for c in (refs.get("nodes") or [])
                    if isinstance(c, dict) and isinstance(c.get("number"), int)
                )
                page = refs.get("pageInfo") or {}
            if isinstance(number, int) and isinstance(head_sha, str):
                # `id` (issue #43): the PR NODE id, the write target for the
                # round/cap marker comments. addComment takes a subjectId, and
                # nothing else in this payload can supply one.
                out.append({
                    "id": node_id if isinstance(node_id, str) else None,
                    "number": number, "head_sha": head_sha, "closes": closes,
                })
        return out

    async def fetch_open_prs_repo_wide(self) -> list[dict]:
        """Every open PR in the repo (issue #192: the digest's PR section).

        Returns `[{"number", "title", "url", "is_draft", "head_ref",
        "updated_at"}]`, oldest first. Read-only, and deliberately NOT a
        generalization of `fetch_open_prs`: that one is head-ref-scoped, selects
        the closing-issue references the handoff validator needs, and paginates
        them per PR — a repo-wide sweep of that selection would spend a nested
        page per PR to answer a question the digest never asks.
        """
        owner, name = self._split_repo()
        out: list[dict] = []
        after: str | None = None
        while True:
            data = await self._request(
                OPEN_PRS_QUERY, {"owner": owner, "name": name, "after": after}
            )
            conn = ((data.get("repository") or {}).get("pullRequests")) or {}
            nodes = conn.get("nodes")
            page_info = conn.get("pageInfo")
            if nodes is None or page_info is None:
                raise TrackerError(
                    "github_unknown_payload", "missing pullRequests nodes/pageInfo"
                )
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(node.get("number"), int):
                    continue
                out.append({
                    "number": node["number"],
                    "title": node.get("title") or "",
                    "url": node.get("url"),
                    "is_draft": bool(node.get("isDraft")),
                    "head_ref": node.get("headRefName") or "",
                    "updated_at": _parse_iso8601(node.get("updatedAt")),
                })
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if after is None:
                raise TrackerError(
                    "github_missing_end_cursor", "hasNextPage true but endCursor missing"
                )
        return out

    async def fetch_pr_comments(self, pr_number: int) -> list[IssueComment]:
        """Top-level (conversation) comments on one PR (issue #43).

        `fetch_issue_comments` resolves `repository.issue(number:)`, which is
        null for a PR number and raises — so the round-marker reader needs its
        own query against `repository.pullRequest(number:)`. Reactions are not
        selected: no marker reader looks at them.
        """
        owner, name = self._split_repo()
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._request(
                PR_COMMENTS_QUERY,
                {"owner": owner, "name": name, "number": int(pr_number), "after": after},
            )
            pr = (data.get("repository") or {}).get("pullRequest")
            if pr is None:
                raise TrackerError(
                    "github_unknown_payload",
                    f"pull request #{pr_number} not found in repository payload",
                )
            conn = pr.get("comments")
            if not isinstance(conn, dict):
                raise TrackerError(
                    "github_unknown_payload", "missing 'pullRequest.comments' connection"
                )
            page_nodes = conn.get("nodes")
            page_info = conn.get("pageInfo")
            if page_nodes is None or page_info is None:
                raise TrackerError(
                    "github_unknown_payload", "missing PR comments nodes/pageInfo"
                )
            nodes.extend(n for n in page_nodes if isinstance(n, dict))
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if after is None:
                raise TrackerError(
                    "github_missing_end_cursor", "hasNextPage true but endCursor missing"
                )
        return [self._normalize_comment(node) for node in nodes]

    async def fetch_pr_review_threads(self, pr_number: int) -> list[ReviewThread]:
        """Review threads on one PR, with each thread's comments (issue #43).

        Read-only. Both connections paginate: a PR can carry more than one page
        of threads, and a long-running argument more than one page of comments
        inside a thread. Truncating either would make `needs_response` read a
        partial history — the exact class of silent wrongness the predicate must
        not have (a missed last-bot-comment reads as "already answered").
        """
        owner, name = self._split_repo()
        raw_threads: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._request(
                PR_REVIEW_THREADS_QUERY,
                {"owner": owner, "name": name, "number": int(pr_number), "after": after},
            )
            pr = (data.get("repository") or {}).get("pullRequest")
            if pr is None:
                raise TrackerError(
                    "github_unknown_payload",
                    f"pull request #{pr_number} not found in repository payload",
                )
            conn = pr.get("reviewThreads")
            if not isinstance(conn, dict):
                raise TrackerError(
                    "github_unknown_payload",
                    "missing 'pullRequest.reviewThreads' connection",
                )
            page_nodes = conn.get("nodes")
            page_info = conn.get("pageInfo")
            if page_nodes is None or page_info is None:
                raise TrackerError(
                    "github_unknown_payload", "missing reviewThreads nodes/pageInfo"
                )
            raw_threads.extend(n for n in page_nodes if isinstance(n, dict))
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if after is None:
                raise TrackerError(
                    "github_missing_end_cursor", "hasNextPage true but endCursor missing"
                )

        threads: list[ReviewThread] = []
        for raw in raw_threads:
            cconn = raw.get("comments") or {}
            comment_nodes = list(cconn.get("nodes") or [])
            page = cconn.get("pageInfo") or {}
            while page.get("hasNextPage"):
                cursor = page.get("endCursor")
                if cursor is None:
                    raise TrackerError(
                        "github_missing_end_cursor",
                        "hasNextPage true but endCursor missing",
                    )
                more = await self._request(
                    REVIEW_THREAD_COMMENTS_PAGE_QUERY,
                    {"threadId": raw.get("id"), "cursor": cursor},
                )
                cconn = ((more.get("node") or {}).get("comments")) or {}
                comment_nodes.extend(cconn.get("nodes") or [])
                page = cconn.get("pageInfo") or {}
            threads.append(
                ReviewThread(
                    id=str(raw.get("id") or ""),
                    is_resolved=bool(raw.get("isResolved")),
                    comments=tuple(
                        self._normalize_review_comment(node)
                        for node in comment_nodes
                        if isinstance(node, dict)
                    ),
                )
            )
        return threads

    async def set_sole_status_label(
        self,
        issue_id: str,
        label: str,
        expected_status: tuple[str, ...] = ("status:in-progress", "status:todo"),
    ) -> None:
        """Issue #61: the orchestrator-owned terminal handoff transition.

        Make `label` the issue's ONLY `status:*` label — one tracker mutation
        from the orchestrator's perspective (add + removals inside), followed
        by a mandatory read-back verification: a textual claim of a transition
        is not evidence the transition happened (issue #44 failure class).
        Raises TrackerError("handoff_label_verify_failed") when the read-back
        does not show exactly `[label]`.
        """
        issues = await self.fetch_issue_states_by_ids([issue_id])
        if not issues:
            raise TrackerError(
                "handoff_label_verify_failed",
                f"issue {issue_id} not fetchable before status swap",
            )
        # Preemption guard (PR #115 review round 4): a human transition or
        # closure landing between provider success and this fetch must WIN —
        # never clobber it. The swap proceeds only from the expected active
        # claim state: an open issue whose sole status label is one of
        # `expected_status`.
        fetched = issues[0]
        if fetched.state.lower() == "closed":
            raise TrackerError(
                "handoff_preempted",
                "issue was closed before the handoff swap; leaving it alone",
            )
        current_status = sorted(
            l for l in fetched.labels if l.startswith("status:")
        )
        if len(current_status) != 1 or current_status[0] not in expected_status:
            raise TrackerError(
                "handoff_preempted",
                f"issue status {current_status} is not an expected active "
                f"claim state {sorted(expected_status)}; a newer transition "
                f"wins and the handoff is refused",
            )
        current = fetched.labels
        added_now = label not in current
        if added_now:
            try:
                await self.add_labels(issue_id, [label])
            except TrackerError as exc:
                # The add is commit-ambiguous too (PR #115 round 6): it may
                # have applied with the response lost, leaving a dual state
                # normalization resolves to human-review — suppressing the
                # retry that would repair it. Read back before deciding.
                try:
                    check = await self.fetch_issue_states_by_ids([issue_id])
                except TrackerError as read_exc:
                    raise TrackerError(
                        "handoff_swap_uncertain",
                        f"label add failed and the read-back also failed; no "
                        f"further mutation attempted — inspect the issue by "
                        f"hand: add={exc}, readback={read_exc}",
                    ) from exc
                applied = bool(check) and label in check[0].labels
                if not applied:
                    raise  # clean failure: nothing mutated, retry can rerun
                # The add landed despite the error — continue the swap.
        stale = [l for l in current if l.startswith("status:") and l != label]
        if stale:
            try:
                await self.remove_labels(issue_id, stale)
            except TrackerError as exc:
                # A failed removal is COMMIT-AMBIGUOUS (PR #115 round 5): the
                # mutation may have applied with the response lost. Blindly
                # rolling back the added label would then strand the issue
                # with NO status label — invisible to fetch_candidate_issues,
                # worse than dual. Read back before compensating.
                try:
                    check = await self.fetch_issue_states_by_ids([issue_id])
                except TrackerError as read_exc:
                    raise TrackerError(
                        "handoff_swap_uncertain",
                        f"stale-removal failed and the read-back also failed; "
                        f"no further mutation attempted — inspect the issue "
                        f"by hand: swap={exc}, readback={read_exc}",
                    ) from exc
                after = check[0].labels if check else []
                status_after = sorted(
                    l for l in after if l.startswith("status:")
                )
                if status_after == [label]:
                    # The removal actually applied. Before accepting, apply
                    # the closure yield here too (PR #115 round 8): this
                    # early return skips the final state check, and a closure
                    # landing by now must win, not be logged as success.
                    if check and check[0].state.lower() == "closed":
                        try:
                            await self.remove_labels(issue_id, [label])
                        except TrackerError:
                            pass
                        raise TrackerError(
                            "handoff_preempted",
                            "issue closed before the ambiguous-removal "
                            "read-back; closure wins, handoff label "
                            "withdrawn (best-effort)",
                        ) from exc
                    # Swap complete on an open issue.
                    return
                # Removal genuinely did not apply: partial swap (dual state —
                # the handoff label the swap adds outranks the claim label it
                # failed to remove, so the issue would derive the GATE state and
                # stop being dispatched, round 3). Compensate by removing the
                # label we just added so
                # the issue returns to its pre-call state and can retry.
                if added_now and label in after:
                    try:
                        await self.remove_labels(issue_id, [label])
                    except TrackerError as rollback_exc:
                        raise TrackerError(
                            "handoff_label_rollback_failed",
                            f"partial swap AND rollback failed; issue is in a "
                            f"dual-status state needing operator repair: "
                            f"swap={exc}, rollback={rollback_exc}",
                        ) from exc
                raise

        # Final read-back with bounded retry (PR #115 round 7): both
        # mutations may have applied — abandoning the handoff on a transient
        # read failure would leave a completed board transition unlogged (the
        # checkpoint asserts the success line). Ambiguity after retries is
        # named, not silent.
        verify = None
        read_exc: TrackerError | None = None
        for _ in range(3):
            try:
                verify = await self.fetch_issue_states_by_ids([issue_id])
                break
            except TrackerError as exc:
                read_exc = exc
        if verify is None:
            raise TrackerError(
                "handoff_swap_uncertain",
                f"both label mutations were issued but the final read-back "
                f"failed 3x; the board may already show the transition — "
                f"inspect by hand: {read_exc}",
            ) from read_exc
        fetched_final = verify[0] if verify else None
        after = fetched_final.labels if fetched_final else []
        # Closure yield (PR #115 round 7): a human closing the issue between
        # the preemption check and here is a terminal transition that WINS —
        # never report a successful handoff on a closed issue. Compensate our
        # label best-effort (label noise on a closed issue is cosmetic).
        if fetched_final is not None and fetched_final.state.lower() == "closed":
            if label in after:
                try:
                    await self.remove_labels(issue_id, [label])
                except TrackerError:
                    pass
            raise TrackerError(
                "handoff_preempted",
                "issue was closed during the swap; the closure wins and the "
                "handoff label was withdrawn (best-effort)",
            )
        status_after = sorted(l for l in after if l.startswith("status:"))
        if status_after != [label]:
            # A concurrent transition landed between the preemption fetch and
            # here (PR #115 round 6): e.g. a human moved in-progress -> todo
            # mid-swap, so we removed only the stale label captured earlier
            # and the issue now shows their status alongside ours — where
            # ours wins normalization and suppresses their intent. The other
            # actor's transition wins: remove OUR label, then report
            # preemption so the session ends without claiming success.
            if label in after and len(status_after) > 1:
                try:
                    await self.remove_labels(issue_id, [label])
                except TrackerError as rollback_exc:
                    raise TrackerError(
                        "handoff_label_rollback_failed",
                        f"concurrent transition during swap AND rollback "
                        f"failed; dual-status state needs operator repair: "
                        f"{status_after}, rollback={rollback_exc}",
                    )
                raise TrackerError(
                    "handoff_preempted",
                    f"concurrent transition landed during the swap "
                    f"({status_after}); our label was removed and the newer "
                    f"transition wins",
                )
            raise TrackerError(
                "handoff_label_verify_failed",
                f"post-swap status labels {status_after} != [{label!r}]",
            )

    async def _resolve_label_id(self, name: str) -> str:
        if name in self._label_id_cache:
            return self._label_id_cache[name]
        owner, repo = self._split_repo()
        data = await self._request(
            LABEL_ID_QUERY, {"owner": owner, "name": repo, "label": name}
        )
        label = (data.get("repository") or {}).get("label")
        if not isinstance(label, dict) or not label.get("id"):
            raise TrackerError(
                "github_label_not_found",
                f"label {name!r} does not exist in the repo (provision it first)",
            )
        self._label_id_cache[name] = label["id"]
        return label["id"]

    # --- pagination -----------------------------------------------------------

    async def _paginate(self, query: str, variables: dict[str, Any]) -> list[dict[str, Any]]:
        """Follow `pageInfo` across pages, preserving order (core §11.2)."""
        results: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            page_vars = dict(variables, after=after)
            data = await self._request(query, page_vars)
            repository = data.get("repository")
            if repository is None or "issues" not in repository:
                raise TrackerError("github_unknown_payload", "missing 'repository.issues' in response data")
            issues_conn = repository["issues"]
            nodes = issues_conn.get("nodes")
            page_info = issues_conn.get("pageInfo")
            if nodes is None or page_info is None:
                raise TrackerError("github_unknown_payload", "missing issues nodes/pageInfo")
            results.extend(nodes)
            has_next = page_info.get("hasNextPage")
            end_cursor = page_info.get("endCursor")
            if not has_next:
                break
            if end_cursor is None:
                raise TrackerError("github_missing_end_cursor", "hasNextPage true but endCursor missing")
            after = end_cursor
        return results

    # --- shared transport (owned extension: graph-review adapter) ------------

    async def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Run one GraphQL request through the vetted transport, returning `data`.

        Public passthrough over `_request` so out-of-band tools (the read-only
        graph-review analyzer, issue #37) can reuse this adapter's auth, timeout,
        and error mapping without re-implementing transport. It performs no
        writes itself — the caller owns query construction. The core §11.5
        no-tracker-writes boundary is about the *scheduler*; a separate,
        manually-invoked analyzer reusing the transport does not cross it.
        """
        return await self._request(query, variables)

    # --- transport --------------------------------------------------------------

    async def _post(self, query: str, variables: dict[str, Any]) -> httpx.Response:
        """One authenticated GraphQL POST. Raises TrackerError on transport
        failure; returns the raw response (status handled by the caller so a
        401 can drive a token re-mint)."""
        try:
            token = await self._creds.token()
            return await self._client.post(
                self._cfg.endpoint,
                json={"query": query, "variables": variables},
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise TrackerError("github_api_request", f"transport error: {exc.__class__.__name__}") from exc

    async def _request(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """POST one GraphQL request; return the `data` object or raise TrackerError.

        The token is fetched per request from the provider (issue #10: App
        installation tokens expire hourly, so a token resolved once at startup
        would 401 mid-run). On a 401, invalidate the cached token and retry
        exactly once — the fresh mint recovers an expiry-boundary race; for a
        static token the invalidate is a no-op and the retry just confirms.

        Never logs or includes the token in error messages (core §11.4).
        """
        response = await self._post(query, variables)
        if response.status_code == 401:
            self._creds.invalidate()
            response = await self._post(query, variables)

        if response.status_code != 200:
            raise TrackerError("github_api_status", f"unexpected status {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise TrackerError("github_unknown_payload", "response body is not valid JSON") from exc

        if not isinstance(payload, dict):
            raise TrackerError("github_unknown_payload", "response body is not a JSON object")

        errors = payload.get("errors")
        if errors:
            raise TrackerError("github_graphql_errors", f"{len(errors)} graphql error(s)")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise TrackerError("github_unknown_payload", "response missing 'data' object")

        return data

    def _split_repo(self) -> tuple[str, str]:
        owner, _, name = self._cfg.repo.partition("/")
        return owner, name

    # --- normalization (core §11.3 + SPEC.md §2) -------------------------------

    @staticmethod
    def _normalize_issue(raw: dict[str, Any]) -> Issue:
        gh_state = raw.get("state")
        label_nodes = (raw.get("labels") or {}).get("nodes") or []
        labels = [n["name"].strip().lower() for n in label_nodes]

        closed = gh_state == "CLOSED"
        present = status_labels(labels)
        if not closed and len(present) > 1:
            # One status label per issue is the workflow contract for STAGE
            # labels; the winner among several is now a stated decision rather
            # than an alphabetical accident (issue #167), so this line reports
            # WHICH state the committed precedence picked instead of warning
            # that the pick was arbitrary. Derivation itself is delegated to the
            # shared normalize_status_state helper.
            #
            # This is a note about THIS derivation, not a finding: markers count
            # here (`parked` + `triage` really does derive `parked`), and the
            # question "is this label set invalid?" belongs to board_sanity,
            # which owns the judgment, the config, and the comment.
            log("issue carries multiple status:* labels; "
                "resolved by the committed precedence",
                issue_number=raw.get("number"), labels=",".join(present),
                state=normalize_status_state(labels, closed=closed))
        state = normalize_status_state(labels, closed=closed)

        blocked_by = [
            BlockerRef(
                id=node.get("id"),
                identifier=str(node["number"]) if node.get("number") is not None else None,
                state="closed" if node.get("state") == "CLOSED" else "open",
            )
            for node in ((raw.get("blockedBy") or {}).get("nodes") or [])
        ]

        return Issue(
            id=raw["id"],
            identifier=str(raw["number"]),
            title=raw.get("title") or "",
            description=raw.get("body"),
            priority=None,
            state=state,
            branch_name=None,
            url=raw.get("url"),
            labels=labels,
            blocked_by=blocked_by,
            created_at=_parse_iso8601(raw.get("createdAt")),
            updated_at=_parse_iso8601(raw.get("updatedAt")),
        )

    @staticmethod
    def _normalize_comment(raw: dict[str, Any]) -> IssueComment:
        """Normalize one comment node (issue #51 part a).

        `author`/`user` are null for deleted accounts, so logins stay
        `str | None` — a null login can never match the operator allowlist,
        which is the safe direction.
        """
        author = raw.get("author") or {}
        login = author.get("login") if isinstance(author, dict) else None
        reactions = []
        for node in ((raw.get("reactions") or {}).get("nodes") or []):
            if not isinstance(node, dict) or not node.get("id"):
                continue
            user = node.get("user") or {}
            reactor = user.get("login") if isinstance(user, dict) else None
            reactions.append(
                CommentReaction(
                    id=node["id"],
                    content=str(node.get("content") or ""),
                    login=reactor.strip().lower() if isinstance(reactor, str) else None,
                    created_at=_parse_iso8601(node.get("createdAt")),
                )
            )
        return IssueComment(
            id=raw["id"],
            body=raw.get("body") or "",
            login=login.strip().lower() if isinstance(login, str) else None,
            created_at=_parse_iso8601(raw.get("createdAt")),
            reactions=tuple(reactions),
        )

    @staticmethod
    def _normalize_review_comment(raw: dict[str, Any]) -> ReviewThreadComment:
        """Normalize one review-thread comment node (issue #43).

        Same null-author posture as `_normalize_comment`: a deleted account
        yields `login=None`, which can match neither the bot allowlist nor the
        Switchboard identity — the safe direction for both conjuncts of
        `needs_response`.
        """
        author = raw.get("author") or {}
        login = author.get("login") if isinstance(author, dict) else None
        return ReviewThreadComment(
            id=str(raw.get("id") or ""),
            login=login.strip().lower() if isinstance(login, str) else None,
            created_at=_parse_iso8601(raw.get("createdAt")),
        )


def _parse_iso8601(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)
