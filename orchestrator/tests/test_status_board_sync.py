"""Tests for the status label <-> Project v2 Status field sync (issue #22).

The sync's live surfaces (webhook delivery, the schedule trigger, a real board)
only exist on the default branch, so what is testable here is the part that
actually carries the risk: the derived honored-drag allowlist, the mirror's
state derivation, and the poll's ARBITRATION branch — which of a field/label
divergence's two causes (a human drag vs. a label write whose mirror has not
landed) the poll concludes, and therefore whether it rewrites a human's label.

Fake fidelity (OBS-023): `FakeProject` never hard-codes what the real server
computes. It ECHOES writes — setting an option bumps that value's `updatedAt`,
a label write appends a timeline event and bumps its timestamp — and derives
every state from labels through `normalize_status_state`, exactly as the
production path does.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

SELF_REPO = "colin-prologue/Switchboard"

from orchestrator.status_board import (
    CLEAR,
    LABEL_WRITE,
    MIRROR,
    NOOP,
    SKIP,
    SNAP_BACK,
    STATE_TO_OPTION,
    honored_drags,
    is_honored_drag,
    label_for_state,
    load_active_states,
    main,
    mirror_decision,
    normalize_state,
    poll_decision,
    run_backfill,
    run_mirror,
    run_poll,
)
from orchestrator.tracker import normalize_status_state
from orchestrator.transitions import load_edges

_REPO_ROOT = Path(__file__).resolve().parents[2]
# Staged, not installed: the App token that pushes this branch has no
# `workflows` permission, so a file under .github/workflows/ cannot be created
# from a worker session. A human moves it there at the merge gate; the tests
# follow it either way so they keep passing after the move.
_INSTALLED_YAML = _REPO_ROOT / ".github" / "workflows" / "status-board-sync.yml"
_STAGED_YAML = _REPO_ROOT / "deploy" / "github-workflows" / "status-board-sync.yml"
_WORKFLOW_YAML = _INSTALLED_YAML if _INSTALLED_YAML.is_file() else _STAGED_YAML

# `gh label list` snapshot, 2026-08-13. The Status field's options must match
# this set exactly (the field carries every status, including ones no drag can
# produce, so label -> field mirroring is total).
LIVE_STATUS_LABELS = {
    "status:blocked",
    "status:decision",
    "status:drafting",
    "status:human-review",
    "status:in-progress",
    "status:parked",
    "status:plan-review",
    "status:todo",
    "status:triage",
}

# The COMPLETE honored set, pinned verbatim. A transitions.yml edit that changes
# the derived set fails this and forces a deliberate re-derivation.
HONORED = frozenset({
    ("todo", "plan review"),
    ("plan review", "drafting"),
    ("decision", "drafting"),
})

# `gh issue list --state open --json number,labels` snapshot, 2026-08-13 — the
# finite backfill expectation. Multi-label rows are NOT hand-resolved here; the
# test resolves them through normalize_status_state, the same function the
# orchestrator uses.
OPEN_ISSUE_SNAPSHOT: dict[int, list[str]] = {
    138: ["status:drafting"],
    135: ["status:drafting"],
    133: ["status:human-review"],
    131: ["status:todo"],
    130: ["status:human-review"],
    112: ["status:todo", "status:parked"],
    106: ["status:todo", "status:parked"],
    56: ["status:drafting"],
    52: ["status:drafting"],
    39: ["status:drafting"],
    38: ["status:drafting"],
    32: ["status:human-review"],
    31: ["status:todo", "status:parked"],
    22: ["status:in-progress"],
    16: ["status:drafting"],
    15: ["status:drafting"],
    12: ["status:drafting"],
}


# --- fake --------------------------------------------------------------------


class FakeProject:
    """A Project v2 board + issue labels, echoing writes the way the server does."""

    def __init__(self, items: list[dict]):
        self._items = [dict(i) for i in items]
        self._tick = 0
        # Every write goes through _now(), so ordering in the fake reflects the
        # ordering the real server would produce.
        for item in self._items:
            item.setdefault("is_issue", True)
            item.setdefault("closed", False)
            item.setdefault("option", None)
            item.setdefault("field_updated_at", None)
            item.setdefault("label_events", [])
        self.label_writes: list[tuple[int, str, tuple[str, ...]]] = []
        self.option_writes: list[tuple[str, str]] = []

    def _now(self) -> str:
        self._tick += 1
        return f"2026-08-13T10:{self._tick:02d}:00Z"

    def stamp_label_event(self, number: int) -> str:
        """Record a labeled/unlabeled timeline event, as the server would."""
        item = self._by_issue(number)
        ts = self._now()
        item["label_events"].append(ts)
        return ts

    def _by_issue(self, number: int) -> dict:
        for item in self._items:
            if item.get("number") == number:
                return item
        raise KeyError(number)

    # -- ProjectClient
    def option_ids(self) -> dict[str, str]:
        # Derived from the module's mapping, never a second hand-written table.
        return {name: f"opt-{name}" for name in STATE_TO_OPTION.values()}

    def items(self) -> list[dict]:
        return [dict(i) for i in self._items]

    def item_for_issue(self, number: int) -> dict | None:
        try:
            return dict(self._by_issue(number))
        except KeyError:
            return None

    def last_label_event_at(self, number: int) -> str | None:
        events = self._by_issue(number)["label_events"]
        return events[-1] if events else None

    def set_option(self, item_id: str, option_id: str) -> None:
        name = {v: k for k, v in self.option_ids().items()}[option_id]
        for item in self._items:
            if item["item_id"] == item_id:
                item["option"] = name
                item["field_updated_at"] = self._now()  # server-assigned echo
        self.option_writes.append((item_id, name))

    def clear_option(self, item_id: str) -> None:
        for item in self._items:
            if item["item_id"] == item_id:
                item["option"] = None
                item["field_updated_at"] = None  # the value node is gone
        self.option_writes.append((item_id, ""))

    def status_labels(self, number: int) -> list[str]:
        return [l for l in self._by_issue(number)["labels"] if l.startswith("status:")]

    def field_option(self, item_id: str):
        for item in self._items:
            if item["item_id"] == item_id:
                return item.get("option")
        raise KeyError(item_id)

    def set_status_label(self, number: int, add: str, remove) -> None:
        item = self._by_issue(number)
        labels = [l for l in item["labels"] if l not in set(remove)]
        if add not in labels:
            labels.append(add)
        item["labels"] = labels
        item["label_events"].append(self._now())
        self.label_writes.append((number, add, tuple(remove)))


def _item(number: int, labels: list[str], **kw) -> dict:
    return {"item_id": f"item-{number}", "number": number, "labels": labels, **kw}


# --- the Status field's options ----------------------------------------------


def test_options_match_the_live_status_label_set():
    assert {label_for_state(s) for s in STATE_TO_OPTION} == LIVE_STATUS_LABELS


def test_option_names_round_trip_through_the_derived_state():
    # The option mapping is keyed off the DERIVED state, so the dash/space
    # rewrite happens once, in tracker.py — never a second time here.
    for label in LIVE_STATUS_LABELS:
        state = normalize_status_state([label], closed=False)
        assert state in STATE_TO_OPTION, label


# --- the derived honored-drag allowlist --------------------------------------


def test_honored_set_is_exactly_three_edges():
    assert honored_drags(repo=SELF_REPO) == HONORED


def test_honored_edges_satisfy_the_three_safety_claims():
    active = load_active_states()
    for src, dst in honored_drags(repo=SELF_REPO):
        assert dst not in active, f"{src}->{dst} lands in an active state"
        assert dst not in {"parked", "closed", "human review"}, f"{src}->{dst}"
        assert "in progress" not in (src, dst), f"{src}->{dst} touches in-progress"
        assert src != "parked", f"{src}->{dst} leaves parked"


def test_degraded_edge_is_not_honored():
    # todo -> human-review is a degraded-path bookkeeping edge encoding a
    # FAILURE mode, never a human intent.
    assert ("todo", "human review") not in honored_drags(repo=SELF_REPO)
    assert not is_honored_drag("todo", "human-review", repo=SELF_REPO)


def test_inactive_edges_are_not_honored():
    """#31 activated the last of #29's phase-2 rows, so the table may now carry
    no inactive edge at all — the invariant is conditional, not the population.
    (Was `test_inactive_phase_two_edges_are_not_honored`, which pinned the
    population and would have failed for the right reason.)"""
    inactive = {
        (normalize_state(e["from"]), normalize_state(e["to"]))
        for e in load_edges()
        if e.get("active", True) is False
    }
    assert not (inactive & honored_drags(repo=SELF_REPO))


def test_fail_review_drags_are_never_honored():
    """`fail review` is a state a SESSION RUNS IN. Dragging a card out of it
    would end the diagnosing session at the next turn boundary and land the
    ticket somewhere with no verdict — the one outcome the feature exists to
    prevent. The generic filter would otherwise honor `fail review -> drafting`,
    since drafting is neither active nor otherwise excluded (issue #31)."""
    for target in ("todo", "drafting", "parked"):
        assert not is_honored_drag("fail review", target, repo=SELF_REPO)
        assert not is_honored_drag("fail-review", target, repo=SELF_REPO)
    # ...and nothing on the board may make an issue enter the diagnosis either.
    assert not is_honored_drag("todo", "fail review", repo=SELF_REPO)


def test_human_gate_exits_into_orchestrator_territory_are_not_honored():
    # The `actor: human` column is the WRONG predicate for drag safety.
    assert not is_honored_drag("parked", "todo", repo=SELF_REPO)
    assert not is_honored_drag("human-review", "todo", repo=SELF_REPO)
    assert not is_honored_drag("plan-review", "in-progress", repo=SELF_REPO)


def test_dashed_and_spaced_spellings_agree():
    assert normalize_state("in-progress") == "in progress"
    assert is_honored_drag("plan-review", "drafting", repo=SELF_REPO)
    assert is_honored_drag("plan review", "drafting", repo=SELF_REPO)


# --- label -> field (the unconditional mirror) -------------------------------


@pytest.mark.parametrize("label", sorted(LIVE_STATUS_LABELS))
def test_mirror_maps_every_label_to_its_option(label):
    decision = mirror_decision([label], closed=False, current_option=None)
    assert decision.action == MIRROR
    assert decision.option == STATE_TO_OPTION[normalize_status_state([label], closed=False)]


def test_mirror_compare_before_write_no_ops():
    # The `labeled` echo run reads equal values and writes nothing.
    decision = mirror_decision(["status:todo"], closed=False, current_option="Todo")
    assert decision.action == NOOP
    assert not decision.writes


def test_mirror_of_a_two_label_issue_takes_the_sorted_first_option():
    # todo + parked shows *Parked* — the same answer the scheduler gets from
    # normalize_status_state. The board may never disagree with tracker.py.
    decision = mirror_decision(
        ["status:todo", "status:parked"], closed=False, current_option=None
    )
    assert decision.option == "Parked"
    assert decision.option == STATE_TO_OPTION[
        normalize_status_state(["status:todo", "status:parked"], closed=False)
    ]


def test_mirror_clears_the_field_when_no_status_label_remains():
    # Reachable mid-relabel: `unlabeled` fires between two separate gh calls.
    decision = mirror_decision([], closed=False, current_option="Todo")
    assert decision.action == CLEAR
    assert mirror_decision([], closed=False, current_option=None).action == NOOP


def test_mirror_skips_closed_issues():
    assert mirror_decision(
        ["status:todo"], closed=True, current_option="Todo"
    ).action == SKIP


# --- the poll's arbitration branch -------------------------------------------


def _poll(labels, option, field_ts, label_ts, **kw):
    return poll_decision(
        labels=labels,
        closed=kw.pop("closed", False),
        current_option=option,
        field_updated_at=field_ts,
        last_label_event_at=label_ts,
        **{"repo": SELF_REPO, **kw},
    )


def test_field_value_newer_plus_honored_pair_writes_the_label():
    # (i) drag todo -> Plan review, field value stamped after the label event.
    decision = _poll(
        ["status:todo"], "Plan review", "2026-08-13T10:05:00Z", "2026-08-13T10:01:00Z"
    )
    assert decision.action == LABEL_WRITE
    assert decision.label == "status:plan-review"
    assert decision.remove_labels == ("status:todo",)


def test_label_event_newer_re_mirrors_and_never_writes_a_label():
    # (ii) the same divergence, read the other way: a label write whose mirror
    # run has not landed.
    decision = _poll(
        ["status:todo"], "Plan review", "2026-08-13T10:01:00Z", "2026-08-13T10:05:00Z"
    )
    assert decision.action == MIRROR
    assert decision.option == "Todo"
    assert decision.label is None


# (iii) the three honored edges READ BACKWARDS: real label changes that produce
# exactly the divergence an honored drag produces. Honoring these would revert a
# human's label edit, so the label must SURVIVE.
@pytest.mark.parametrize(
    "old_state,new_state",
    [
        ("plan review", "todo"),      # backwards read of todo -> plan review
        ("drafting", "plan review"),  # backwards read of plan review -> drafting
        ("drafting", "decision"),     # backwards read of decision -> drafting
    ],
)
def test_honored_edges_read_backwards_preserve_the_label(old_state, new_state):
    new_label = label_for_state(new_state)
    fake = FakeProject([
        _item(
            1,
            [new_label],
            option=STATE_TO_OPTION[old_state],  # stale mirror, not a drag
            field_updated_at="2026-08-13T10:01:00Z",
            label_events=["2026-08-13T10:05:00Z"],
        )
    ])
    # Sanity: the pair, read as a drag, WOULD be honored.
    assert is_honored_drag(new_state, old_state, repo=SELF_REPO)

    [decision] = run_poll(fake, repo=SELF_REPO)

    assert decision.action == MIRROR
    assert fake.label_writes == []
    assert fake.items()[0]["labels"] == [new_label]
    assert fake.items()[0]["option"] == STATE_TO_OPTION[new_state]


def test_item_with_no_status_value_is_a_mirror_not_a_drag():
    # (iv) every item's state the instant it joins the board: no value node, no
    # updatedAt to compare. Reading one off a null would crash the poll.
    decision = _poll(["status:todo"], None, None, "2026-08-13T10:05:00Z")
    assert decision.action == MIRROR
    assert decision.option == "Todo"

    fake = FakeProject([_item(1, ["status:todo"], label_events=["2026-08-13T10:00:00Z"])])
    assert run_poll(fake, repo=SELF_REPO)[0].action == MIRROR
    assert fake.items()[0]["option"] == "Todo"


def test_missing_label_event_defaults_to_the_label_winning():
    # Snapping back is recoverable; honoring a phantom drag rewrites a human.
    decision = _poll(["status:todo"], "Plan review", "2026-08-13T10:05:00Z", None)
    assert decision.action == MIRROR
    assert decision.label is None


@pytest.mark.parametrize(
    "label,dragged_to",
    [
        ("status:drafting", "Todo"),          # would make the issue dispatchable
        ("status:drafting", "In progress"),   # would collide with the claim
        ("status:in-progress", "Drafting"),   # would kill a running session
        ("status:parked", "Todo"),            # unpark must stay a label action
        ("status:todo", "Human review"),      # Gate C is orchestrator-owned
        ("status:todo", "Blocked"),           # blocked has no edges at all
        ("status:blocked", "Todo"),
    ],
)
def test_unhonored_drags_snap_back(label, dragged_to):
    decision = _poll(
        [label], dragged_to, "2026-08-13T10:05:00Z", "2026-08-13T10:01:00Z"
    )
    assert decision.action == SNAP_BACK
    assert decision.option == STATE_TO_OPTION[normalize_status_state([label], closed=False)]
    assert decision.label is None


def test_snap_back_is_applied_as_a_field_write_only():
    fake = FakeProject([
        _item(
            1,
            ["status:drafting"],
            option="Todo",
            field_updated_at="2026-08-13T10:05:00Z",
            label_events=["2026-08-13T10:01:00Z"],
        )
    ])
    [decision] = run_poll(fake, repo=SELF_REPO)
    assert decision.action == SNAP_BACK
    assert fake.label_writes == []
    assert fake.items()[0]["option"] == "Drafting"


def test_honored_drag_applied_end_to_end_swaps_the_label():
    fake = FakeProject([
        _item(
            1,
            ["status:todo", "gate:triage-passed"],
            option="Plan review",
            field_updated_at="2026-08-13T10:05:00Z",
            label_events=["2026-08-13T10:01:00Z"],
        )
    ])
    [decision] = run_poll(fake, repo=SELF_REPO)
    assert decision.action == LABEL_WRITE
    assert fake.label_writes == [(1, "status:plan-review", ("status:todo",))]
    labels = fake.items()[0]["labels"]
    assert "status:plan-review" in labels and "status:todo" not in labels
    assert "gate:triage-passed" in labels  # non-status labels are untouched

    # The echo: the mirror run that the label write triggers reads equal values
    # and writes nothing (the loop terminates).
    assert run_mirror(fake, 1).action == NOOP


# --- the poll's domain -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs,fragment",
    [
        ({"is_issue": False}, "not an issue"),
        ({"closed": True}, "closed"),
        ({"labels": []}, "0 status"),
        ({"labels": ["status:todo", "status:parked"]}, "2 status"),
    ],
)
def test_poll_skips_everything_outside_its_domain(kwargs, fragment):
    base = dict(
        labels=["status:todo"],
        closed=False,
        is_issue=True,
        current_option="Drafting",
        field_updated_at="2026-08-13T10:05:00Z",
        last_label_event_at="2026-08-13T10:01:00Z",
    )
    base.update(kwargs)
    decision = poll_decision(**base)
    assert decision.action == SKIP
    assert fragment in decision.reason


def test_poll_no_ops_when_field_and_label_agree():
    decision = _poll(
        ["status:todo"], "Todo", "2026-08-13T10:05:00Z", "2026-08-13T10:01:00Z"
    )
    assert decision.action == NOOP


# --- backfill ----------------------------------------------------------------


def test_backfill_covers_the_open_issue_snapshot():
    fake = FakeProject([
        _item(n, list(labels)) for n, labels in OPEN_ISSUE_SNAPSHOT.items()
    ])
    run_backfill(fake)
    got = {i["number"]: i["option"] for i in fake.items()}
    expected = {
        n: STATE_TO_OPTION[normalize_status_state(labels, closed=False)]
        for n, labels in OPEN_ISSUE_SNAPSHOT.items()
    }
    assert got == expected
    # Three rows in this snapshot carry two status labels; each resolves to the
    # sorted-first option rather than being ambiguous.
    assert expected[112] == expected[106] == expected[31] == "Parked"


def test_backfill_skips_non_issue_items():
    fake = FakeProject([{"item_id": "pr-1", "number": 9, "labels": [], "is_issue": False}])
    assert run_backfill(fake)[0].action == SKIP
    assert fake.option_writes == []


# --- the CLI entry point (argparse + the --dry-run branch) -------------------


def _dry_run_fixture() -> FakeProject:
    return FakeProject([
        _item(
            1,
            ["status:todo"],
            option="Plan review",
            field_updated_at="2026-08-13T10:05:00Z",
            label_events=["2026-08-13T10:01:00Z"],
        )
    ])


def test_main_dry_run_poll_decides_but_writes_nothing(capsys):
    fake = _dry_run_fixture()
    assert main(["--mode", "poll", "--dry-run", "--repo", SELF_REPO], client=fake) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out and "action=label_write" in out
    assert fake.label_writes == [] and fake.option_writes == []


def test_main_dry_run_mirror_targets_one_issue(capsys):
    fake = _dry_run_fixture()
    assert main(["--mode", "mirror", "--issue", "1", "--dry-run"], client=fake) == 0
    assert "action=snap_back" not in capsys.readouterr().out
    assert fake.option_writes == []


def test_main_mirror_requires_an_issue_number():
    with pytest.raises(SystemExit):
        main(["--mode", "mirror", "--dry-run"], client=FakeProject([]))


def test_main_without_dry_run_applies_writes():
    fake = _dry_run_fixture()
    assert main(["--mode", "poll", "--repo", SELF_REPO], client=fake) == 0
    assert fake.label_writes == [(1, "status:plan-review", ("status:todo",))]


# --- the workflow file -------------------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW_YAML.read_text(encoding="utf-8"))


def test_workflow_trigger_block_is_exactly_the_decided_set():
    raw = _workflow()
    # PyYAML parses the bare `on:` key as the boolean True.
    on = raw.get("on", raw.get(True))
    assert set(on) == {"issues", "schedule", "workflow_dispatch"}
    assert on["issues"]["types"] == ["labeled", "unlabeled"]
    # Without `schedule` the whole field -> label direction is dead on arrival:
    # Actions has no `on:` event for Projects v2.
    assert on["schedule"] == [{"cron": "*/5 * * * *"}]


def test_workflow_serializes_runs_against_each_other():
    raw = _workflow()
    concurrency = raw["concurrency"]
    assert concurrency["group"] == "status-board-sync"
    assert concurrency["cancel-in-progress"] is False


def test_workflow_uses_the_projects_pat_not_the_default_token():
    # Comments are excluded: the header EXPLAINS why GITHUB_TOKEN cannot serve.
    body = "\n".join(
        line for line in _WORKFLOW_YAML.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "secrets.PROJECT_SYNC_PAT" in body
    assert "secrets.GITHUB_TOKEN" not in body


def test_workflow_file_exists_at_exactly_one_of_the_two_paths():
    assert _WORKFLOW_YAML.is_file()
    assert not (_INSTALLED_YAML.is_file() and _STAGED_YAML.is_file()), (
        "two copies of the sync workflow: git rm the staged one after installing"
    )


def test_workflow_header_documents_the_snap_back_and_the_60_day_disable():
    header = "\n".join(
        line for line in _WORKFLOW_YAML.read_text(encoding="utf-8").splitlines()
        if line.startswith("#")
    )
    assert "Blocked" in header          # no edges: rubber-bands every drag
    assert "60 days" in header          # scheduled-workflow auto-disable
    assert "re-enable" in header.lower()


# --- repo scoping + fieldValues truncation (codex review, PR #141) ------------


def _gh_node(number, repo="colin-prologue/Switchboard", *, status=None,
             truncated=False):
    field_values = {"pageInfo": {"hasNextPage": truncated}, "nodes": []}
    if status is not None:
        field_values["nodes"].append(
            {"name": status, "updatedAt": "2026-08-13T00:00:00Z",
             "field": {"name": "Status"}}
        )
    return {
        "id": f"node-{number}",
        "type": "ISSUE",
        "content": {
            "__typename": "Issue",
            "number": number,
            "state": "OPEN",
            "repository": {"nameWithOwner": repo},
            "labels": {"nodes": [{"name": "status:todo"}]},
        },
        "fieldValues": field_values,
    }


def _gh_client_with_nodes(monkeypatch, nodes):
    from orchestrator.status_board import GhClient
    client = GhClient.__new__(GhClient)
    client._owner = "colin-prologue"
    client._project = 1
    client._repo = "colin-prologue/Switchboard"
    client._field_name = "Status"
    payload = {"data": {"user": {"projectV2": {"items": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": nodes,
    }}}}}
    monkeypatch.setattr(client, "_gh", lambda args: payload)
    return client


def test_foreign_repo_items_are_invisible(monkeypatch):
    # Issue numbers collide across repos: the foreign #7 must not shadow or
    # stand in for the local #7 (codex review, PR #141 — a number match alone
    # relabeled an unrelated local issue from foreign labels).
    client = _gh_client_with_nodes(monkeypatch, [
        _gh_node(7, repo="other/elsewhere", status="Todo"),
        _gh_node(7, status="In progress"),
    ])
    items = client.items()
    assert [i["repo"] for i in items] == ["colin-prologue/Switchboard"]
    assert client.item_for_issue(7)["option"] == "In progress"


def test_repo_match_is_case_insensitive(monkeypatch):
    client = _gh_client_with_nodes(monkeypatch, [
        _gh_node(3, repo="Colin-Prologue/switchboard", status="Todo"),
    ])
    assert len(client.items()) == 1


def test_truncated_field_page_reads_indeterminate_not_unset(monkeypatch):
    client = _gh_client_with_nodes(monkeypatch, [
        _gh_node(9, status=None, truncated=True),
    ])
    (item,) = client.items()
    assert item["option"] is None
    assert item["field_page_truncated"] is True


def test_poll_skips_indeterminate_items():
    fake = FakeProject([
        _item(9, ["status:todo"], is_issue=True, closed=False, option=None,
              field_updated_at=None, field_page_truncated=True),
    ])
    decisions = run_poll(fake, dry_run=False, repo=SELF_REPO)
    assert decisions[0].action == SKIP
    assert "indeterminate" in decisions[0].reason
    assert fake.option_writes == []


# --- honored-drag marker effects + TOCTOU guard (codex review, PR #141) -------


def test_honored_drag_applies_edge_marker_effects():
    # decision -> drafting carries remove_marker: gate:triage-passed
    # (workflow/transitions.yml); the honored drag must clear it.
    from orchestrator.status_board import marker_effects
    assert "gate:triage-passed" in marker_effects("decision", "drafting")
    fake = FakeProject([
        _item(5, ["status:decision", "gate:triage-passed"], is_issue=True,
              option="Drafting",
              field_updated_at="2026-08-13T09:00:00Z",
              label_events=["2026-08-13T08:00:00Z"]),
    ])
    run_poll(fake, dry_run=False, repo=SELF_REPO)
    (write,) = fake.label_writes
    assert write[1] == "status:drafting"
    assert "gate:triage-passed" in write[2]


def test_label_write_skips_when_labels_changed_mid_poll(capsys):
    class RacyFake(FakeProject):
        def status_labels(self, number):
            return ["status:parked"]  # concurrent human move
    fake = RacyFake([
        _item(5, ["status:decision"], is_issue=True, option="Drafting",
              field_updated_at="2026-08-13T09:00:00Z",
              label_events=["2026-08-13T08:00:00Z"]),
    ])
    run_poll(fake, dry_run=False, repo=SELF_REPO)
    assert fake.label_writes == []
    assert "changed mid-poll" in capsys.readouterr().out


def test_write_skips_when_field_changed_mid_poll(capsys):
    class RacyFieldFake(FakeProject):
        def field_option(self, item_id):
            return "Parked"  # concurrent drag after the snapshot
    fake = RacyFieldFake([
        _item(6, ["status:todo"], is_issue=True, option=None,
              field_updated_at=None),
    ])
    run_poll(fake, dry_run=False, repo=SELF_REPO)
    assert fake.option_writes == []
    assert "field changed mid-poll" in capsys.readouterr().out


def test_truncated_label_page_is_indeterminate():
    fake = FakeProject([
        _item(7, ["status:todo"], is_issue=True, option=None,
              field_updated_at=None, labels_truncated=True),
    ])
    decisions = run_poll(fake, dry_run=False, repo=SELF_REPO)
    assert decisions[0].action == SKIP
    assert fake.option_writes == [] and fake.label_writes == []
    fake2 = FakeProject([
        _item(7, ["status:todo"], is_issue=True, option="Todo",
              labels_truncated=True),
    ])
    d = run_mirror(fake2, 7, dry_run=False)
    assert d.action == SKIP


# --- the honored set is a PER-PROJECT question (issue #156) -------------------
#
# `active_states` became a stance property in AgDR-039. Deriving the exclusion
# set from a fixed template applies one project's state machine to every board:
# `base` has `triage` and no `review`, `prototype` the reverse. These pin that
# the board asks the project it is actually syncing.

def _binding(tmp_path, slug, repo, active, handoff="status:human-review"):
    proj = tmp_path / "projects" / slug
    proj.mkdir(parents=True)
    (proj / "project.env").write_text(
        f"SB_PROJECT_SLUG={slug}\nSB_GITHUB_REPO={repo}\nSB_BASE_BRANCH=main\n")
    states = ", ".join(f'"{s}"' for s in active)
    (proj / "WORKFLOW.md").write_text(
        "---\ntracker:\n"
        f"  active_states: [{states}]\n"
        f'  handoff_label: "{handoff}"\n'
        "---\nbody\n")
    return proj


def test_workflow_is_resolved_from_the_repo_binding(tmp_path):
    from orchestrator.status_board import workflow_for_repo

    _binding(tmp_path, "alpha", "acme/alpha", ["triage", "todo", "in progress"])
    _binding(tmp_path, "beta", "acme/beta", ["todo", "in progress", "review"])

    assert workflow_for_repo("acme/beta", tmp_path / "projects").parent.name == "beta"
    # GitHub repo names are case-insensitive; a case-sensitive match would send a
    # real project down the unresolved (snap-back-everything) path.
    assert workflow_for_repo("ACME/Beta", tmp_path / "projects").parent.name == "beta"
    assert workflow_for_repo("acme/never-registered", tmp_path / "projects") is None
    assert workflow_for_repo("", tmp_path / "projects") is None


def test_two_stances_produce_different_exclusion_sets(tmp_path):
    """The bug in one assertion: a base-shaped and a prototype-shaped project
    must not be arbitrated against the same set of orchestrator-owned states."""
    from orchestrator.status_board import load_active_states

    base = _binding(tmp_path, "alpha", "acme/alpha", ["triage", "todo", "in progress"])
    proto = _binding(tmp_path, "beta", "acme/beta", ["todo", "in progress", "review"],
                     handoff="status:review")

    base_states = load_active_states(base / "WORKFLOW.md")
    proto_states = load_active_states(proto / "WORKFLOW.md")
    assert base_states != proto_states
    assert "triage" in base_states and "triage" not in proto_states
    assert "review" in proto_states and "review" not in base_states


def test_unresolved_project_snaps_back_everything(tmp_path, monkeypatch):
    """Fail CLOSED. Honoring a drag writes a `status:*` label the orchestrator
    dispatches on; refusing one only annoys a human. An unknown project must
    never become the permissive case."""
    import orchestrator.status_board as sb

    monkeypatch.setattr(sb, "PROJECTS_DIR", tmp_path / "projects")
    assert sb.honored_drags(repo="acme/never-registered") == frozenset()
    assert sb.honored_drags(repo="") == frozenset()
    assert not sb.is_honored_drag("todo", "plan review", repo="acme/never-registered")


def test_a_projects_own_active_state_is_never_an_honored_target(tmp_path, monkeypatch):
    """The property the exclusion set exists for, asserted per stance rather
    than against a fixed template: nothing may drag an issue INTO a state that
    project's orchestrator dispatches."""
    import orchestrator.status_board as sb

    monkeypatch.setattr(sb, "PROJECTS_DIR", tmp_path / "projects")
    _binding(tmp_path, "beta", "acme/beta", ["todo", "in progress", "review"],
             handoff="status:review")

    for _, dst in sb.honored_drags(repo="acme/beta"):
        assert dst not in {"todo", "in progress", "review"}, (
            f"a drag into {dst!r} was honored on a project that dispatches it")
