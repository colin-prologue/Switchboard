"""Tests for the GitHub tracker adapter.

implements: core §17.3 (Issue Tracker Client test matrix), adapted for the
GitHub binding per SPEC.md §2 / SPEC.md §4 owned extension.

All HTTP is mocked via httpx.MockTransport — no network access.
"""

from __future__ import annotations

import json

import httpx
import pytest

from orchestrator.tracker import (
    ADD_COMMENT_MUTATION,
    ADD_LABELS_MUTATION,
    CANDIDATE_ISSUES_QUERY,
    CLOSED_ISSUES_QUERY,
    ISSUES_BY_IDS_QUERY,
    LABEL_ID_QUERY,
    GitHubTracker,
)
from orchestrator.types import TrackerConfig, TrackerError


def make_cfg(**overrides) -> TrackerConfig:
    defaults = dict(
        kind="github",
        repo="acme/widgets",
        endpoint="https://api.github.com/graphql",
        api_key="test-token-123",
        required_labels=[],
        active_states=["todo", "in progress"],
        terminal_states=["closed"],
    )
    defaults.update(overrides)
    return TrackerConfig(**defaults)


def issue_node(
    id_="I_1",
    number=1,
    title="Some issue",
    body="desc",
    url="https://github.com/acme/widgets/issues/1",
    state="OPEN",
    created_at="2024-01-01T00:00:00Z",
    updated_at="2024-01-02T00:00:00Z",
    labels=None,
    blocked_by=None,
    include_blocked_by=True,
):
    node = {
        "id": id_,
        "number": number,
        "title": title,
        "body": body,
        "url": url,
        "state": state,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
    }
    if include_blocked_by:
        node["blockedBy"] = {"nodes": blocked_by or []}
    return node


def graphql_response(data=None, errors=None, status_code=200):
    body: dict = {}
    if data is not None:
        body["data"] = data
    if errors is not None:
        body["errors"] = errors
    return httpx.Response(status_code, json=body)


class RecordingTransport(httpx.MockTransport):
    """MockTransport wrapper that counts calls and captures requests."""

    def __init__(self, handler):
        self.calls: list[httpx.Request] = []

        def wrapped(request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            return handler(request)

        super().__init__(wrapped)

    @property
    def call_count(self) -> int:
        return len(self.calls)


def make_tracker(handler, cfg: TrackerConfig | None = None) -> tuple[GitHubTracker, RecordingTransport]:
    transport = RecordingTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    tracker = GitHubTracker(cfg or make_cfg(), client=client)
    return tracker, transport


def request_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode())


# --- fetch_candidate_issues ---------------------------------------------------


@pytest.mark.asyncio
async def test_candidate_fetch_filters_to_active_states_via_status_labels():
    todo_issue = issue_node(id_="I_1", number=1, labels=["status:todo"])
    gated_issue = issue_node(id_="I_2", number=2, labels=["status:human-review"])
    in_progress_issue = issue_node(id_="I_3", number=3, labels=["status:in-progress"])

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": [todo_issue, gated_issue, in_progress_issue],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

    tracker, _ = make_tracker(handler)
    issues = await tracker.fetch_candidate_issues()

    assert {i.identifier for i in issues} == {"1", "3"}
    assert all(i.state in ("todo", "in progress") for i in issues)


@pytest.mark.asyncio
async def test_pagination_across_two_pages_preserves_order():
    page1 = [issue_node(id_="I_1", number=1, labels=["status:todo"])]
    page2 = [issue_node(id_="I_2", number=2, labels=["status:todo"])]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = request_body(request)
        after = body["variables"].get("after")
        if after is None:
            return graphql_response(
                {
                    "repository": {
                        "issues": {
                            "nodes": page1,
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                }
            )
        assert after == "cursor-1"
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": page2,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_candidate_issues()

    assert [i.identifier for i in issues] == ["1", "2"]
    assert transport.call_count == 2


@pytest.mark.asyncio
async def test_missing_end_cursor_with_has_next_page_raises_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": [issue_node(labels=["status:todo"])],
                        "pageInfo": {"hasNextPage": True, "endCursor": None},
                    }
                }
            }
        )

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_candidate_issues()
    assert excinfo.value.code == "github_missing_end_cursor"


@pytest.mark.asyncio
async def test_labels_are_lowercased_and_trimmed():
    node = issue_node(labels=[" Status:Todo ", "Foo-BAR"])

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": [node],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

    tracker, _ = make_tracker(handler)
    issues = await tracker.fetch_candidate_issues()

    assert len(issues) == 1
    assert issues[0].labels == ["status:todo", "foo-bar"]
    assert issues[0].state == "todo"


@pytest.mark.asyncio
async def test_blocked_by_normalizes_open_and_closed_blockers():
    node = issue_node(
        labels=["status:todo"],
        blocked_by=[
            {"id": "I_open", "number": 10, "state": "OPEN"},
            {"id": "I_closed", "number": 11, "state": "CLOSED"},
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": [node],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

    tracker, _ = make_tracker(handler)
    issues = await tracker.fetch_candidate_issues()

    assert len(issues) == 1
    blockers = {b.identifier: b.state for b in issues[0].blocked_by}
    assert blockers == {"10": "open", "11": "closed"}
    ids = {b.identifier: b.id for b in issues[0].blocked_by}
    assert ids == {"10": "I_open", "11": "I_closed"}


@pytest.mark.asyncio
async def test_closed_issue_state_is_closed_even_with_status_label():
    # Verify the normalizer via fetch_issue_states_by_ids (candidate fetch
    # filters to active states, so a closed issue would never appear there).
    node = issue_node(state="CLOSED", labels=["status:todo"])
    tracker, _ = make_tracker(
        lambda request: graphql_response({"nodes": [node]})
    )
    issues = await tracker.fetch_issue_states_by_ids(["I_1"])
    assert issues[0].state == "closed"


@pytest.mark.asyncio
async def test_multiple_status_labels_pick_deterministic_sorted_first():
    node = issue_node(labels=["status:in-progress", "status:blocked"])

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response({"nodes": [node]})

    tracker, _ = make_tracker(handler)
    issues = await tracker.fetch_issue_states_by_ids(["I_1"])

    # sorted(["status:in-progress", "status:blocked"])[0] == "status:blocked"
    assert issues[0].state == "blocked"


@pytest.mark.asyncio
async def test_no_status_label_yields_state_none():
    node = issue_node(labels=["bug", "priority:high"])

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response({"nodes": [node]})

    tracker, _ = make_tracker(handler)
    issues = await tracker.fetch_issue_states_by_ids(["I_1"])

    assert issues[0].state == "none"


# --- fetch_issues_by_states ----------------------------------------------------


@pytest.mark.asyncio
async def test_empty_fetch_issues_by_states_makes_zero_http_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_issues_by_states([])

    assert issues == []
    assert transport.call_count == 0


@pytest.mark.asyncio
async def test_fetch_issues_by_states_queries_closed_issues():
    node = issue_node(state="CLOSED", labels=["status:todo"], include_blocked_by=False)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        assert body["query"] == CLOSED_ISSUES_QUERY
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": [node],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_issues_by_states(["closed"])

    assert transport.call_count == 1
    assert len(issues) == 1
    assert issues[0].state == "closed"


@pytest.mark.asyncio
async def test_fetch_issues_by_states_non_closed_state_names_return_empty():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_issues_by_states(["some_other_state"])

    assert issues == []
    assert transport.call_count == 0


# --- fetch_issue_states_by_ids -------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_issue_states_by_ids_uses_nodes_query_and_skips_nulls():
    node = issue_node(id_="I_1", number=1, labels=["status:todo"])

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        assert body["query"] == ISSUES_BY_IDS_QUERY
        assert body["variables"] == {"ids": ["I_1", "I_deleted"]}
        # None = deleted issue; {} = a node id that is not an Issue (the
        # `... on Issue` fragment matched nothing). Both must be skipped,
        # never KeyError (an escaped non-TrackerError would strand claims).
        return graphql_response({"nodes": [node, None, {}]})

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_issue_states_by_ids(["I_1", "I_deleted"])

    assert transport.call_count == 1
    assert len(issues) == 1
    assert issues[0].identifier == "1"


@pytest.mark.asyncio
async def test_fetch_issue_states_by_ids_empty_makes_zero_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("should not be called")

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_issue_states_by_ids([])

    assert issues == []
    assert transport.call_count == 0


# --- error mapping --------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_200_status_raises_github_api_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert excinfo.value.code == "github_api_status"


@pytest.mark.asyncio
async def test_graphql_errors_array_raises_github_graphql_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(errors=[{"message": "field not found"}])

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert excinfo.value.code == "github_graphql_errors"


@pytest.mark.asyncio
async def test_transport_error_raises_github_api_request():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert excinfo.value.code == "github_api_request"


@pytest.mark.asyncio
async def test_malformed_payload_raises_github_unknown_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        # 'data' present but missing 'nodes' key entirely
        return graphql_response({"unexpected": "shape"})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert excinfo.value.code == "github_unknown_payload"


@pytest.mark.asyncio
async def test_malformed_payload_missing_data_raises_github_unknown_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nothing": "here"})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert excinfo.value.code == "github_unknown_payload"


@pytest.mark.asyncio
async def test_non_json_body_raises_github_unknown_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json at all")

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as excinfo:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert excinfo.value.code == "github_unknown_payload"


# --- auth header / add_issue_comment -------------------------------------------


@pytest.mark.asyncio
async def test_authorization_header_carries_token():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return graphql_response({"nodes": []})

    cfg = make_cfg(api_key="super-secret-token")
    tracker, _ = make_tracker(handler, cfg=cfg)
    await tracker.fetch_issue_states_by_ids(["I_1"])

    assert captured["auth"] == "Bearer super-secret-token"


@pytest.mark.asyncio
async def test_add_issue_comment_posts_mutation():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request_body(request)
        return graphql_response({"addComment": {"commentEdge": {"node": {"id": "C_1"}}}})

    tracker, transport = make_tracker(handler)
    await tracker.add_issue_comment("I_1", "Parking this issue after 3 sessions.")

    assert transport.call_count == 1
    assert captured["body"]["query"] == ADD_COMMENT_MUTATION
    assert captured["body"]["variables"] == {
        "subjectId": "I_1",
        "body": "Parking this issue after 3 sessions.",
    }


@pytest.mark.asyncio
async def test_add_labels_resolves_id_then_posts_mutation():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        captured.append(body)
        if body["query"] == LABEL_ID_QUERY:
            return graphql_response({"repository": {"label": {"id": "LA_parked"}}})
        return graphql_response({"addLabelsToLabelable": {"clientMutationId": None}})

    tracker, transport = make_tracker(handler)
    await tracker.add_labels("I_1", ["status:parked"])

    assert transport.call_count == 2                      # resolve id, then mutate
    assert captured[0]["query"] == LABEL_ID_QUERY
    assert captured[0]["variables"]["label"] == "status:parked"
    assert captured[1]["query"] == ADD_LABELS_MUTATION
    assert captured[1]["variables"] == {
        "labelableId": "I_1",
        "labelIds": ["LA_parked"],
    }


@pytest.mark.asyncio
async def test_add_labels_caches_label_id_across_calls():
    def handler(request: httpx.Request) -> httpx.Response:
        if request_body(request)["query"] == LABEL_ID_QUERY:
            return graphql_response({"repository": {"label": {"id": "LA_parked"}}})
        return graphql_response({"addLabelsToLabelable": {"clientMutationId": None}})

    tracker, transport = make_tracker(handler)
    await tracker.add_labels("I_1", ["status:parked"])
    await tracker.add_labels("I_2", ["status:parked"])

    # 2 (first call: resolve + mutate) + 1 (second call: mutate only, id cached)
    assert transport.call_count == 3


@pytest.mark.asyncio
async def test_add_labels_raises_when_label_not_provisioned():
    def handler(request: httpx.Request) -> httpx.Response:
        if request_body(request)["query"] == LABEL_ID_QUERY:
            return graphql_response({"repository": {"label": None}})
        return graphql_response({"addLabelsToLabelable": {"clientMutationId": None}})

    tracker, transport = make_tracker(handler)
    with pytest.raises(TrackerError) as exc:
        await tracker.add_labels("I_1", ["status:parked"])

    assert exc.value.code == "github_label_not_found"
    assert transport.call_count == 1                      # never reached the mutation


@pytest.mark.asyncio
async def test_candidate_issues_query_sends_owner_name_and_open_state():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request_body(request)
        return graphql_response(
            {
                "repository": {
                    "issues": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        )

    tracker, _ = make_tracker(handler, cfg=make_cfg(repo="acme/widgets"))
    await tracker.fetch_candidate_issues()

    assert captured["body"]["query"] == CANDIDATE_ISSUES_QUERY
    assert captured["body"]["variables"]["owner"] == "acme"
    assert captured["body"]["variables"]["name"] == "widgets"
    assert captured["body"]["variables"]["states"] == ["OPEN"]
