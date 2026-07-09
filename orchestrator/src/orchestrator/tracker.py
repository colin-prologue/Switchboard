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
from typing import Any

import httpx

from orchestrator.auth import AppInstallationTokenProvider, StaticTokenProvider
from orchestrator.log import log
from orchestrator.types import BlockerRef, Issue, TrackerConfig, TrackerError

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


def normalize_status_state(labels: list[str], *, closed: bool) -> str:
    """Derive the workflow state from an issue's labels (SPEC.md §2).

    The single source of truth for status:* -> state mapping so test fakes can
    assert fidelity against it instead of hard-coding states: closed issues are
    terminal "closed"; otherwise the sorted-first `status:*` label wins (one
    status label per issue is the contract; ties resolve deterministically),
    "status:" is stripped and "-" -> " "; no status label -> "none".

    Labels are assumed already normalized (stripped, lower-cased) as
    `_normalize_issue` produces them. Pure — the multi-label diagnostic lives in
    `_normalize_issue`, which has the issue number to log.
    """
    if closed:
        return "closed"
    status_labels = sorted(l for l in labels if l.startswith("status:"))
    if status_labels:
        return status_labels[0][len("status:") :].replace("-", " ")
    return "none"


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

    async def fetch_candidate_issues(self) -> list[Issue]:
        """Open issues in the repo, filtered to `cfg.active_states` post-normalization.

        implements: core §11.1(1) / overridden by: SPEC.md §2 (repo instead of
        project_slug; state from status:* labels instead of first-class state)
        """
        owner, name = self._split_repo()
        raw_issues = await self._paginate(
            CANDIDATE_ISSUES_QUERY, {"owner": owner, "name": name, "states": ["OPEN"]}
        )
        issues = [self._normalize_issue(raw) for raw in raw_issues]
        active = set(self._cfg.active_states)
        return [issue for issue in issues if issue.state in active]

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
        if not closed and len([l for l in labels if l.startswith("status:")]) > 1:
            # One status label per issue is the workflow contract; more than one
            # resolves deterministically (sorted-first) but the winner is
            # semantically arbitrary — surface it. Derivation itself is delegated
            # to the shared normalize_status_state helper.
            status_labels = sorted(l for l in labels if l.startswith("status:"))
            log("issue carries multiple status:* labels; using sorted-first",
                issue_number=raw.get("number"), labels=",".join(status_labels))
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


def _parse_iso8601(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value)
