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
    ISSUE_COMMENTS_QUERY,
    ISSUES_BY_IDS_QUERY,
    LABEL_ID_QUERY,
    REMOVE_LABELS_MUTATION,
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
async def test_remove_labels_resolves_id_then_posts_mutation():
    """issue #14: pins the real removeLabelsFromLabelable request shape
    independently of the fake (OBS-023) — mirrors the add_labels contract test."""
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request_body(request)
        captured.append(body)
        if body["query"] == LABEL_ID_QUERY:
            return graphql_response({"repository": {"label": {"id": "LA_inprog"}}})
        return graphql_response({"removeLabelsFromLabelable": {"clientMutationId": None}})

    tracker, transport = make_tracker(handler)
    await tracker.remove_labels("I_1", ["status:in-progress"])

    assert transport.call_count == 2                      # resolve id, then mutate
    assert captured[0]["query"] == LABEL_ID_QUERY
    assert captured[0]["variables"]["label"] == "status:in-progress"
    assert captured[1]["query"] == REMOVE_LABELS_MUTATION
    assert captured[1]["variables"] == {
        "labelableId": "I_1",
        "labelIds": ["LA_inprog"],
    }


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


# --- credential provider / 401 re-mint (issue #10) ----------------------------


class FakeCreds:
    """Hands out tokens from a list, advancing on invalidate(); lets a test
    drive the tracker's 401-refresh contract without minting a real App token."""

    def __init__(self, tokens: list[str]):
        self._tokens = tokens
        self.invalidations = 0
        self.handed: list[str] = []

    async def token(self) -> str:
        tok = self._tokens[min(self.invalidations, len(self._tokens) - 1)]
        self.handed.append(tok)
        return tok

    def invalidate(self) -> None:
        self.invalidations += 1


def make_tracker_with_creds(handler, creds):
    transport = RecordingTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    tracker = GitHubTracker(make_cfg(), client=client, creds=creds)
    return tracker, transport


@pytest.mark.asyncio
async def test_request_sends_provider_token():
    creds = FakeCreds(["ghs_minted"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer ghs_minted"
        return graphql_response({"nodes": []})

    tracker, _ = make_tracker_with_creds(handler, creds)
    await tracker.fetch_issue_states_by_ids(["I_1"])
    assert creds.handed == ["ghs_minted"]


@pytest.mark.asyncio
async def test_401_invalidates_and_retries_once_with_fresh_token():
    # First call 401s (stale hourly token); after invalidate() the second call
    # carries a freshly-minted token and succeeds. Models a run crossing the
    # installation token's hourly expiry.
    creds = FakeCreds(["stale", "fresh"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return graphql_response({"nodes": []})

    tracker, transport = make_tracker_with_creds(handler, creds)
    await tracker.fetch_issue_states_by_ids(["I_1"])

    assert len(transport.calls) == 2
    assert creds.invalidations == 1
    assert creds.handed == ["stale", "fresh"]


@pytest.mark.asyncio
async def test_persistent_401_gives_up_after_one_retry():
    # The re-mint doesn't fix it (e.g. revoked installation): exactly one
    # retry, then a github_api_status error. No infinite loop.
    creds = FakeCreds(["a", "b"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    tracker, transport = make_tracker_with_creds(handler, creds)
    with pytest.raises(TrackerError) as exc:
        await tracker.fetch_issue_states_by_ids(["I_1"])

    assert exc.value.code == "github_api_status"
    assert len(transport.calls) == 2
    assert creds.invalidations == 1


@pytest.mark.asyncio
async def test_default_creds_fall_back_to_cfg_api_key():
    # No provider wired (standalone/test use): the tracker authenticates with
    # the statically-resolved cfg.api_key, exactly as before issue #10.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token-123"
        return graphql_response({"nodes": []})

    transport = RecordingTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    tracker = GitHubTracker(make_cfg(), client=client)
    await tracker.fetch_issue_states_by_ids(["I_1"])
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_mint_failure_surfaces_as_tracker_error():
    # The provider raising an httpx error during token() (mint failure) must
    # become a TrackerError, keeping the tracker's only-TrackerError contract.
    class MintFails:
        async def token(self) -> str:
            raise httpx.ConnectError("mint endpoint unreachable")

        def invalidate(self) -> None:
            pass

    tracker, _ = make_tracker_with_creds(
        lambda request: graphql_response({"nodes": []}), MintFails())
    with pytest.raises(TrackerError) as exc:
        await tracker.fetch_issue_states_by_ids(["I_1"])
    assert exc.value.code == "github_api_request"


# --- issue #61: set_sole_status_label partial-swap rollback -------------------


@pytest.mark.asyncio
async def test_sole_status_swap_rolls_back_added_label_when_remove_fails():
    """PR #115 round 3: add-then-remove-fails must not strand a dual-status
    issue (sorted-first normalization would deactivate it). The method removes
    the label it just added, restoring the pre-call state, then raises."""
    state = {"remove_calls": 0, "added": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "removeLabelsFromLabelable" in body:
            state["remove_calls"] += 1
            if state["remove_calls"] == 1:
                # stale-removal fails AND genuinely does not apply
                return graphql_response(errors=[{"message": "boom"}])
            return graphql_response(data={"removeLabelsFromLabelable": {"clientMutationId": None}})
        if "addLabelsToLabelable" in body:
            state["add_ids"] = json.loads(body)["variables"]["labelIds"]
            state["added"] = True
            return graphql_response(data={"addLabelsToLabelable": {"clientMutationId": None}})
        if "label(name:" in body or '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        # issue state fetch — stateful: shows the applied add so the round-5
        # read-back sees the true dual state and proceeds to rollback
        labels = ["status:in-progress", "gate:triage-passed"]
        if state["added"]:
            labels = ["status:in-progress", "status:human-review", "gate:triage-passed"]
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1, labels=labels,
        )]})

    tracker, transport = make_tracker(handler)
    with pytest.raises(TrackerError):
        await tracker.set_sole_status_label("node-1", "status:human-review")
    # remove was attempted twice: the failing stale-removal, then the rollback
    assert state["remove_calls"] == 2
    # the rollback removed exactly the label the method had just added
    assert state["add_ids"] == ["id-status:human-review"]


@pytest.mark.asyncio
async def test_sole_status_swap_refuses_when_human_transition_preempted():
    """PR #115 round 4: a human moving the issue (or closing it) between
    provider success and the swap fetch must win — no mutations."""
    mutations = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "Labelable" in body:
            mutations["count"] += 1
            return graphql_response(data={})
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1,
            labels=["status:drafting"],  # human re-scoped it mid-turn
        )]})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as exc:
        await tracker.set_sole_status_label("node-1", "status:human-review")
    assert exc.value.code == "handoff_preempted"
    assert mutations["count"] == 0


@pytest.mark.asyncio
async def test_fetch_open_prs_paginates_closing_references():
    """PR #115 round 4: closing refs beyond the first page must be collected,
    not truncated into a false pr_linkage_missing."""
    state = {"pages": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if "pullRequests(headRefName" in request.content.decode():
            return graphql_response(data={"repository": {"pullRequests": {"nodes": [{
                "number": 9,
                "headRefOid": "abc",
                "closingIssuesReferences": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    "nodes": [{"number": n} for n in range(1, 51)],
                },
            }]}}})
        state["pages"] += 1
        assert body["variables"]["prNumber"] == 9
        return graphql_response(data={"repository": {"pullRequest": {
            "closingIssuesReferences": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"number": 61}],
            },
        }}})

    tracker, _ = make_tracker(handler)
    prs = await tracker.fetch_open_prs("switchboard/issue-61")
    assert state["pages"] == 1
    assert prs[0]["number"] == 9
    assert 61 in prs[0]["closes"] and 50 in prs[0]["closes"]


@pytest.mark.asyncio
async def test_sole_status_swap_detects_applied_removal_despite_error():
    """PR #115 round 5: a stale-removal that APPLIED but errored (lost
    response) must be detected by read-back and treated as a completed swap —
    never rolled back into a no-status stranded issue."""
    state = {"removed": False, "remove_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "removeLabelsFromLabelable" in body:
            state["remove_calls"] += 1
            state["removed"] = True  # the mutation APPLIES...
            return graphql_response(errors=[{"message": "response lost"}])  # ...but errors
        if "addLabelsToLabelable" in body:
            return graphql_response(data={"addLabelsToLabelable": {"clientMutationId": None}})
        if '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        labels = ["gate:triage-passed", "status:human-review"] if state["removed"] \
            else ["status:in-progress", "gate:triage-passed"]
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1, labels=labels,
        )]})

    tracker, _ = make_tracker(handler)
    # completes without raising: read-back shows the swap landed
    await tracker.set_sole_status_label("node-1", "status:human-review")
    assert state["remove_calls"] == 1  # no rollback removal was attempted


@pytest.mark.asyncio
async def test_sole_status_swap_continues_when_ambiguous_add_actually_applied():
    """PR #115 round 6: an add that APPLIED but errored must not abort the
    swap into a dual state — read-back detects it and the swap completes."""
    state = {"added": False, "removed": False, "add_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "addLabelsToLabelable" in body:
            state["add_calls"] += 1
            state["added"] = True  # applies...
            return graphql_response(errors=[{"message": "response lost"}])  # ...but errors
        if "removeLabelsFromLabelable" in body:
            state["removed"] = True
            return graphql_response(data={"removeLabelsFromLabelable": {"clientMutationId": None}})
        if '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        labels = ["status:in-progress", "gate:triage-passed"]
        if state["added"]:
            labels = ["status:human-review", "gate:triage-passed"] if state["removed"] \
                else ["status:in-progress", "status:human-review", "gate:triage-passed"]
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1, labels=labels,
        )]})

    tracker, _ = make_tracker(handler)
    await tracker.set_sole_status_label("node-1", "status:human-review")
    assert state["add_calls"] == 1 and state["removed"]


@pytest.mark.asyncio
async def test_sole_status_swap_yields_to_concurrent_transition_at_verify():
    """PR #115 round 6: a human transition landing mid-swap wins — the swap
    removes its own label and reports preemption, never leaving human-review
    to suppress the human's status."""
    state = {"remove_calls": [], "phase": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "addLabelsToLabelable" in body:
            return graphql_response(data={"addLabelsToLabelable": {"clientMutationId": None}})
        if "removeLabelsFromLabelable" in body:
            state["remove_calls"].append(json.loads(body)["variables"]["labelIds"])
            return graphql_response(data={"removeLabelsFromLabelable": {"clientMutationId": None}})
        if '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        state["phase"] += 1
        if state["phase"] == 1:  # preemption fetch: expected claim state
            labels = ["status:in-progress", "gate:triage-passed"]
        else:  # verify fetch: human moved it to todo mid-swap
            labels = ["status:todo", "status:human-review", "gate:triage-passed"]
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1, labels=labels,
        )]})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as exc:
        await tracker.set_sole_status_label("node-1", "status:human-review")
    assert exc.value.code == "handoff_preempted"
    # final removal was OUR label, yielding to the human's status:todo
    assert state["remove_calls"][-1] == ["id-status:human-review"]


@pytest.mark.asyncio
async def test_sole_status_swap_retries_transient_final_readback():
    """PR #115 round 7: a transient final read-back failure must not abandon
    a completed swap — bounded retry recovers the success path."""
    state = {"phase": 0, "verify_failures": 0, "removed": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "addLabelsToLabelable" in body:
            return graphql_response(data={"addLabelsToLabelable": {"clientMutationId": None}})
        if "removeLabelsFromLabelable" in body:
            state["removed"] = True
            return graphql_response(data={"removeLabelsFromLabelable": {"clientMutationId": None}})
        if '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        state["phase"] += 1
        if state["phase"] == 1:  # preemption fetch
            return graphql_response(data={"nodes": [issue_node(
                id_="node-1", number=1,
                labels=["status:in-progress", "gate:triage-passed"])]})
        if state["verify_failures"] < 2:  # first two read-backs: transient failure
            state["verify_failures"] += 1
            return graphql_response(errors=[{"message": "timeout"}])
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1,
            labels=["status:human-review", "gate:triage-passed"])]})

    tracker, _ = make_tracker(handler)
    await tracker.set_sole_status_label("node-1", "status:human-review")
    assert state["verify_failures"] == 2 and state["removed"]


@pytest.mark.asyncio
async def test_sole_status_swap_yields_when_issue_closes_mid_swap():
    """PR #115 round 7: closure between the preemption check and the final
    read-back wins — no success on a closed issue, our label withdrawn."""
    state = {"phase": 0, "remove_calls": []}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "addLabelsToLabelable" in body:
            return graphql_response(data={"addLabelsToLabelable": {"clientMutationId": None}})
        if "removeLabelsFromLabelable" in body:
            state["remove_calls"].append(json.loads(body)["variables"]["labelIds"])
            return graphql_response(data={"removeLabelsFromLabelable": {"clientMutationId": None}})
        if '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        state["phase"] += 1
        if state["phase"] == 1:
            return graphql_response(data={"nodes": [issue_node(
                id_="node-1", number=1,
                labels=["status:in-progress", "gate:triage-passed"])]})
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1, state="CLOSED",
            labels=["status:human-review", "gate:triage-passed"])]})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as exc:
        await tracker.set_sole_status_label("node-1", "status:human-review")
    assert exc.value.code == "handoff_preempted"
    assert state["remove_calls"][-1] == ["id-status:human-review"]


@pytest.mark.asyncio
async def test_ambiguous_removal_readback_yields_to_closure():
    """PR #115 round 8: the ambiguous-removal early return must apply the
    closure yield too — an applied-but-errored removal read back on a closed
    issue is preemption, not success."""
    state = {"removed": False, "remove_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "removeLabelsFromLabelable" in body:
            state["remove_calls"] += 1
            if state["remove_calls"] == 1:
                state["removed"] = True  # applies but errors
                return graphql_response(errors=[{"message": "response lost"}])
            return graphql_response(data={"removeLabelsFromLabelable": {"clientMutationId": None}})
        if "addLabelsToLabelable" in body:
            return graphql_response(data={"addLabelsToLabelable": {"clientMutationId": None}})
        if '"label"' in body:
            name = json.loads(body)["variables"]["label"]
            return graphql_response(data={"repository": {"label": {"id": f"id-{name}"}}})
        if not state["removed"]:  # preemption fetch: open, expected claim
            return graphql_response(data={"nodes": [issue_node(
                id_="node-1", number=1,
                labels=["status:in-progress", "gate:triage-passed"])]})
        # ambiguity read-back: removal applied AND the issue closed meanwhile
        return graphql_response(data={"nodes": [issue_node(
            id_="node-1", number=1, state="CLOSED",
            labels=["status:human-review", "gate:triage-passed"])]})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as exc:
        await tracker.set_sole_status_label("node-1", "status:human-review")
    assert exc.value.code == "handoff_preempted"
    assert state["remove_calls"] == 2  # withdrawal of our label on the closed issue


# --- fetch_open_issues_by_status (issue #51 part a) ---------------------------
#
# Gate states (drafting/decision) are NOT active states, so fetch_candidate_issues
# structurally never returns them, and fetch_issues_by_states is hard-wired to the
# CLOSED startup query. This method is the only way the fold poller can see its
# issue set — and it must apply the SAME client-side status filter.

@pytest.mark.asyncio
async def test_open_issues_by_status_filters_client_side_to_requested_states():
    drafting = issue_node(id_="I_1", number=1, labels=["status:drafting"])
    decision = issue_node(id_="I_2", number=2, labels=["status:decision"])
    todo = issue_node(id_="I_3", number=3, labels=["status:todo"])

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(
            {"repository": {"issues": {
                "nodes": [drafting, decision, todo],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}
        )

    tracker, transport = make_tracker(handler)
    issues = await tracker.fetch_open_issues_by_status(["drafting", "decision"])

    assert [i.identifier for i in issues] == ["1", "2"]
    body = request_body(transport.calls[0])
    # Reuses the candidate query (already parameterized on states) against OPEN.
    assert body["query"] == CANDIDATE_ISSUES_QUERY
    assert body["variables"]["states"] == ["OPEN"]


@pytest.mark.asyncio
async def test_open_issues_by_status_empty_request_makes_no_api_calls():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request expected")

    tracker, transport = make_tracker(handler)
    assert await tracker.fetch_open_issues_by_status([]) == []
    assert transport.call_count == 0


# --- fetch_issue_comments: EXACT query shape (issue #51 part a) ---------------

@pytest.mark.asyncio
async def test_issue_comments_query_requests_the_exact_required_fields():
    """The reaction login field is `user`, NOT `author` (only Comment-ish types
    carry `author`) — asking for the wrong one is a schema error at runtime, so
    the shape is pinned here rather than discovered in production."""
    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(
            {"repository": {"issue": {"comments": {
                "nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}}}
        )

    tracker, transport = make_tracker(handler)
    await tracker.fetch_issue_comments("42")

    body = request_body(transport.calls[0])
    assert body["query"] == ISSUE_COMMENTS_QUERY
    assert body["variables"] == {
        "owner": "acme", "name": "widgets", "number": 42, "after": None,
    }
    q = ISSUE_COMMENTS_QUERY
    assert "$number: Int!" in q
    assert "author { login }" in q                                # comment author
    assert "reactions(first: 100)" in q
    assert "nodes { id content createdAt user { login } }" in q   # reaction login
    assert "reactionGroups" not in q   # presence alone cannot identify a login


@pytest.mark.asyncio
async def test_issue_comments_normalize_author_reactions_and_paginate():
    verdict = {
        "id": "IC_1",
        "body": "## Triage verdict\nbody-sha1: " + "a" * 40,
        "createdAt": "2026-08-01T10:00:00Z",
        "author": {"login": "Verifier-Bot"},
        "reactions": {"nodes": [
            {"id": "RE_1", "content": "THUMBS_UP",
             "createdAt": "2026-08-01T11:00:00Z",
             "user": {"login": "Colin-Prologue"}},
            {"id": "RE_2", "content": "HEART", "createdAt": None, "user": None},
        ]},
    }
    orphan = {
        "id": "IC_2", "body": "/fold", "createdAt": "2026-08-01T12:00:00Z",
        "author": None,  # deleted account
        "reactions": {"nodes": []},
    }

    pages = [
        {"repository": {"issue": {"comments": {
            "nodes": [verdict],
            "pageInfo": {"hasNextPage": True, "endCursor": "cur1"}}}}},
        {"repository": {"issue": {"comments": {
            "nodes": [orphan],
            "pageInfo": {"hasNextPage": False, "endCursor": None}}}}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response(pages[len(transport.calls) - 1])

    tracker, transport = make_tracker(handler)
    comments = await tracker.fetch_issue_comments(42)

    assert [c.id for c in comments] == ["IC_1", "IC_2"]
    assert comments[0].login == "verifier-bot"   # lower-cased for matching
    assert comments[1].login is None             # deleted account never matches
    assert [(r.id, r.content, r.login) for r in comments[0].reactions] == [
        ("RE_1", "THUMBS_UP", "colin-prologue"),
        ("RE_2", "HEART", None),
    ]
    assert request_body(transport.calls[1])["variables"]["after"] == "cur1"


@pytest.mark.asyncio
async def test_issue_comments_missing_issue_raises_tracker_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return graphql_response({"repository": {"issue": None}})

    tracker, _ = make_tracker(handler)
    with pytest.raises(TrackerError) as exc_info:
        await tracker.fetch_issue_comments(999)
    assert exc_info.value.code == "github_unknown_payload"
