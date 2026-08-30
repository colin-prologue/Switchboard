"""status:* label <-> GitHub Project v2 single-select field sync (issue #22).

The board is a WRITABLE lens over the native `status:*` labels, and the contract
between the two directions is DELIBERATELY ASYMMETRIC:

  label -> field  UNCONDITIONAL. Native labels stay the single source of truth,
                  so every label change mirrors to the board. Driven by the
                  repository `issues: [labeled, unlabeled]` webhook.

  field -> label  RESTRICTED to an invariant-derived allowlist of drags
                  (`honored_drags()`). Everything else is SNAPPED BACK — the
                  field is rewritten from the label. Driven by a scheduled poll,
                  because GitHub Actions has NO `on:` event for Projects v2
                  (`projects_v2_item` is an ORGANIZATION webhook event and this
                  repo's owner is a User account).

Two rules carry the whole safety argument, and both are pinned in
`orchestrator/tests/test_status_board_sync.py`:

1. The board may never disagree with `tracker.py` about the same issue, so the
   mirror derives its option from `normalize_status_state` — the orchestrator's
   own "single source of truth for status:* -> state mapping" — rather than
   reimplementing the `status:` strip and the dash->space rewrite. `status:*`
   labels are NOT mutually exclusive in practice (open issues carry two today),
   and both readers must answer the same way. Which one wins is the committed
   precedence in `workflow/transitions.yml` (issue #167) — an explicit ranking,
   not the alphabet — and deriving through `normalize_status_state` is what
   makes the board inherit it for free.

2. A DIVERGENCE IS NOT A DRAG. The same field/label mismatch arises from a human
   label edit whose mirror run has not landed yet, and honoring that backwards
   would REVERT the human. Arbitration is by time, on the FIELD VALUE's own
   `updatedAt` (see `poll_decision`).

This module is pure logic plus a thin `gh` client; `main(["--dry-run", ...])` is
the local entry point and the surface the tests drive.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence

import yaml

from .tracker import normalize_status_state
from .transitions import load_edges

# --- states, labels, options -------------------------------------------------

STATUS_PREFIX = "status:"

WORKFLOW_BASE_PATH = (
    Path(__file__).resolve().parents[3] / "workflow" / "WORKFLOW.base.md"
)

PROJECTS_DIR = Path(__file__).resolve().parents[3] / "projects"

_ACTIVE_STATES_RE = re.compile(r"^\s*active_states:\s*(\[.*\])\s*$")

# The Status field's option per DERIVED STATE (spaces, not dashes — the
# `normalize_status_state` spelling). Keyed off the derived state rather than
# off the label so the dash/space rewrite happens exactly once, in tracker.py.
# The field carries EVERY status so label->field mirroring is total, including
# states no drag can ever produce.
STATE_TO_OPTION: dict[str, str] = {
    "blocked": "Blocked",
    "decision": "Decision",
    "drafting": "Drafting",
    "human review": "Human review",
    "in progress": "In progress",
    "parked": "Parked",
    "plan review": "Plan review",
    "todo": "Todo",
    "triage": "Triage",
}

OPTION_TO_STATE: dict[str, str] = {o: s for s, o in STATE_TO_OPTION.items()}

# A drag out of these states is never honored: `triage`/`in progress`/`fail
# review` are the three states sessions RUN in (a state change ends a role-pinned
# session at the next turn boundary), and leaving `parked` is an unpark, which
# must stay a deliberate label action.
#
# `fail review` joined the list with the state itself (issue #31), and it is the
# same argument, not a new one: dragging a card out of *Fail review* would end
# the diagnosing session mid-run and land the ticket in drafting with no verdict
# — the one outcome the feature exists to prevent. The exclusion is needed
# because the generic filter would otherwise honor `fail review -> drafting`
# (drafting is neither active nor otherwise excluded); the other two routes out
# are already covered, since `todo` is active and `parked` is excluded.
EXCLUDED_FROM_STATES = frozenset({"triage", "in progress", "fail review", "parked"})

# A drag INTO these is never honored: nothing on the board may make an issue
# dispatchable, touch the orchestrator's claim state, park, hand an issue to
# Gate C (`human review` is orchestrator-owned, written only after evidence
# validation), or close it (`closed` has no column at all).
EXCLUDED_TO_EXTRA = frozenset({"parked", "closed", "human review"})


def normalize_state(value: str) -> str:
    """`transitions.yml`'s dashed spelling -> the tracker's spaced spelling.

    `transitions.yml` writes `in-progress`; `active_states` and
    `normalize_status_state` both write `in progress`. Normalizing before set
    membership is the whole fix for that trap.
    """
    return (value or "").strip().lower().replace("-", " ")


def label_for_state(state: str) -> str:
    """Derived state -> the `status:*` label that produces it."""
    return STATUS_PREFIX + normalize_state(state).replace(" ", "-")


def option_for_state(state: str) -> str | None:
    return STATE_TO_OPTION.get(normalize_state(state))


def state_for_option(option: str | None) -> str | None:
    if option is None:
        return None
    return OPTION_TO_STATE.get(option.strip())


def status_labels(labels: Iterable[str]) -> list[str]:
    return sorted(l for l in labels if l.startswith(STATUS_PREFIX))


def workflow_for_repo(repo: str, projects_dir: Path = PROJECTS_DIR) -> Path | None:
    """The COMPOSED workflow of the project bound to `repo`, or None.

    `active_states` became a per-project stance property in `AgDR-039`, so
    deriving the honored-drag set from a fixed template applies one project's
    state machine to every board — `base` has `triage` and no `review`,
    `prototype` the reverse. The board would then arbitrate a project's
    legitimate transitions using another project's rules (issue #156).

    Resolution goes through the same binding the orchestrator dispatches from:
    match `SB_GITHUB_REPO` in each `projects/<slug>/project.env`, then read that
    project's `WORKFLOW.md`. Deriving it rather than declaring a second copy is
    what keeps the board and the scheduler from disagreeing, which is the
    failure `AgDR-043` was written about.
    """
    target = (repo or "").strip().lower()
    if not target:
        return None
    try:
        bindings = sorted(projects_dir.glob("*/project.env"))
    except OSError:
        return None
    for env_file in bindings:
        try:
            text = env_file.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            if line.startswith("SB_GITHUB_REPO="):
                if line.split("=", 1)[1].strip().lower() == target:
                    workflow = env_file.parent / "WORKFLOW.md"
                    return workflow if workflow.is_file() else None
                break
    return None


def load_active_states(path: Path = WORKFLOW_BASE_PATH) -> frozenset[str]:
    """`tracker.active_states` read from the committed workflow front matter.

    Read, not duplicated as a Python literal: the excluded-`to` set is derived
    from it, and a Python copy would drift silently the day a state is added.

    The one line is matched rather than the whole front matter loaded, because
    the base file is a TEMPLATE — its unsubstituted `{{PLACEHOLDER}}` keys are
    flow mappings to a YAML parser, which `yaml.safe_load` refuses as unhashable
    keys. `register-project.sh` substitutes them; nothing here needs to.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ACTIVE_STATES_RE.match(line)
        if match:
            states = yaml.safe_load(match.group(1)) or []
            return frozenset(
                normalize_state(s) for s in states if isinstance(s, str)
            )
    raise ValueError(f"{path} declares no tracker.active_states")


def honored_drags(
    edges: Sequence[dict] | None = None,
    active_states: frozenset[str] | None = None,
    repo: str = "",
) -> frozenset[tuple[str, str]]:
    """The (from, to) pairs a board drag is allowed to write a label for.

    `repo` names the repository being synced; its project's `active_states`
    decide which targets are orchestrator-owned. When the project cannot be
    resolved the honored set is EMPTY — every drag snaps back. That direction is
    deliberate: honoring a drag writes a `status:*` label the orchestrator
    dispatches on, while refusing one only annoys a human, so an unknown project
    must never become the permissive case (issue #156).

    Derived from `workflow/transitions.yml`, never hand-listed: an edge with
    that pair must EXIST (the table carries duplicate `(from, to)` rows, so this
    is existence, not identity), must not be `active: false` (the repo idiom is
    default-active — no row carries a literal `active: true`), must not be
    `degraded: true` (a degraded row encodes a FAILURE mode, never a human
    intent), and both endpoints must clear the exclusion sets above.

    The `actor` column is deliberately NOT the predicate: it records who
    performs a pipeline transition, and its `human` rows include gate exits into
    orchestrator territory (`parked -> todo`, `human-review -> todo`).
    """
    rows = load_edges() if edges is None else edges
    if active_states is None:
        workflow = workflow_for_repo(repo)
        if workflow is None:
            return frozenset()
        active_states = load_active_states(workflow)
    excluded_to = active_states | EXCLUDED_TO_EXTRA

    out: set[tuple[str, str]] = set()
    for edge in rows:
        if edge.get("active", True) is False or edge.get("degraded") is True:
            continue
        src = normalize_state(str(edge.get("from", "")))
        dst = normalize_state(str(edge.get("to", "")))
        if not src or not dst:
            continue
        if src in EXCLUDED_FROM_STATES or dst in excluded_to:
            continue
        out.add((src, dst))
    return frozenset(out)


def is_honored_drag(
    from_state: str,
    to_state: str,
    *,
    allowlist: frozenset[tuple[str, str]] | None = None,
    repo: str = "",
) -> bool:
    pairs = honored_drags(repo=repo) if allowlist is None else allowlist
    return (normalize_state(from_state), normalize_state(to_state)) in pairs


def marker_effects(
    from_state: str, to_state: str, edges: Sequence[dict] | None = None
) -> tuple[str, ...]:
    """`remove_marker` values of the active edges matching (from, to).

    A drag that takes an edge takes the edge's side effects too (codex
    review, PR #141): `decision -> drafting` carries a `remove_marker`, and
    honoring the drag without it leaves a stale provenance marker that lets
    a later `status:todo` edit satisfy the scheduler's gate without a new
    triage pass. Marker names come from the YAML only (the no-literal
    drift guard in test_transitions.py). `add_marker` has no honored-drag
    consumer (its only row is a verifier edge excluded from the honored
    set) and is intentionally not applied here.
    """
    rows = load_edges() if edges is None else edges
    src, dst = normalize_state(from_state), normalize_state(to_state)
    out: list[str] = []
    for edge in rows:
        if edge.get("active", True) is False or edge.get("degraded") is True:
            continue
        if (normalize_state(str(edge.get("from", ""))) == src
                and normalize_state(str(edge.get("to", ""))) == dst):
            rm = edge.get("remove_marker")
            if isinstance(rm, str) and rm and rm not in out:
                out.append(rm)
    return tuple(out)


# --- decisions ---------------------------------------------------------------

# Action verbs. `mirror` and `snap_back` are the SAME write (field := label);
# they differ only in why, and the run log says which.
NOOP = "noop"
SKIP = "skip"
MIRROR = "mirror"
CLEAR = "clear"
SNAP_BACK = "snap_back"
LABEL_WRITE = "label_write"


@dataclass(frozen=True)
class Decision:
    """What the sync should do to one item, and the log line explaining it."""

    action: str
    reason: str
    option: str | None = None       # option to write (mirror / snap_back)
    label: str | None = None        # status:* label to write (label_write)
    remove_labels: tuple[str, ...] = ()
    from_state: str | None = None
    to_state: str | None = None

    @property
    def writes(self) -> bool:
        return self.action in {MIRROR, CLEAR, SNAP_BACK, LABEL_WRITE}


def mirror_decision(
    labels: Iterable[str],
    *,
    closed: bool,
    current_option: str | None,
) -> Decision:
    """label -> field. Unconditional, with one compare-before-write no-op.

    The state comes from `normalize_status_state`, so a two-label issue mirrors
    to its SORTED-FIRST label and the board agrees with the scheduler about the
    same issue. Zero `status:*` labels -> state "none" -> the value is CLEARED:
    that is reachable mid-relabel, because `unlabeled` fires between two
    separate `gh` calls and the following `labeled` event restores it.
    """
    labels = list(labels)
    state = normalize_status_state(labels, closed=closed)

    if state == "closed":
        # The board's domain is open issues; a closed issue has no column and
        # clearing would destroy the last known status. Leave the card alone.
        return Decision(SKIP, "issue is closed; board mirrors open issues only")

    if state == "none":
        if current_option is None:
            return Decision(NOOP, "no status label and no field value")
        return Decision(
            CLEAR,
            "no status:* label; clearing (restored by the next labeled event)",
        )

    option = option_for_state(state)
    if option is None:
        return Decision(SKIP, f"no Status option for state {state!r}")
    if current_option == option:
        return Decision(NOOP, f"field already {option!r}")
    return Decision(
        MIRROR,
        f"label state {state!r} -> option {option!r}",
        option=option,
        to_state=state,
    )


def poll_decision(
    *,
    labels: Iterable[str],
    closed: bool,
    is_issue: bool = True,
    current_option: str | None,
    field_updated_at: str | None,
    last_label_event_at: str | None,
    allowlist: frozenset[tuple[str, str]] | None = None,
    repo: str = "",
) -> Decision:
    """One project ITEM's arbitration on the scheduled poll.

    Domain (everything else is skipped with a log line rather than falling into
    an undefined branch that would write to the board): the item's content is an
    ISSUE, that issue is OPEN, and it carries EXACTLY ONE `status:*` label.

    Arbitration, once a divergence exists:

    * no Status value at all — every item's state the instant it joins the board
      — is a MIRROR, never a drag. There is no value node and no `updatedAt` to
      compare, and reading one off a null crashes the write direction.
    * the FIELD VALUE's own `updatedAt` newer than the issue's most recent
      `labeled`/`unlabeled` timeline event => a drag. The value node is scoped
      to that field on that item, so an issue-side label write cannot
      contaminate it. The ITEM's `updatedAt` is explicitly NOT the signal (its
      scope is undocumented and plausibly bumps on linked-issue edits, which
      would make this a tautology returning "drag" in exactly the failed-mirror
      case it guards). `creator` is not the signal either: under one personal
      PAT the mirror and the drag are the same actor.
    * otherwise the label is newer — a label write whose mirror has not landed —
      and the poll RE-MIRRORS it. No label write.

    A drag that clears arbitration is still honored only if its (from, to) is in
    the derived allowlist; anything else snaps back.
    """
    labels = list(labels)

    if not is_issue:
        return Decision(SKIP, "item content is not an issue (PR or draft)")
    if closed:
        return Decision(SKIP, "linked issue is closed")

    present = status_labels(labels)
    if len(present) != 1:
        return Decision(
            SKIP,
            f"linked issue carries {len(present)} status:* labels; poll needs exactly one",
        )

    label_state = normalize_status_state(labels, closed=False)
    label_option = option_for_state(label_state)
    if label_option is None:
        return Decision(SKIP, f"no Status option for state {label_state!r}")

    if current_option == label_option:
        return Decision(NOOP, f"field and label agree on {label_option!r}")

    if current_option is None:
        return Decision(
            MIRROR,
            f"item has no Status value; mirroring {label_state!r}",
            option=label_option,
            to_state=label_state,
        )

    field_state = state_for_option(current_option)
    if field_state is None:
        return Decision(
            SNAP_BACK,
            f"unknown option {current_option!r}; rewriting from label",
            option=label_option,
            to_state=label_state,
        )

    if not _is_newer(field_updated_at, last_label_event_at):
        return Decision(
            MIRROR,
            f"label event is newer than the field value; re-mirroring {label_state!r}",
            option=label_option,
            to_state=label_state,
        )

    if is_honored_drag(label_state, field_state, allowlist=allowlist, repo=repo):
        return Decision(
            LABEL_WRITE,
            f"honored drag {label_state!r} -> {field_state!r}",
            label=label_for_state(field_state),
            remove_labels=(label_for_state(label_state),)
            + marker_effects(label_state, field_state),
            from_state=label_state,
            to_state=field_state,
        )

    return Decision(
        SNAP_BACK,
        f"drag {label_state!r} -> {field_state!r} is not in the honored set; snapping back",
        option=label_option,
        from_state=label_state,
        to_state=label_state,
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_newer(field_ts: str | None, label_ts: str | None) -> bool:
    """Is the field value strictly newer than the last label event?

    Either timestamp missing or unparseable => NOT newer. The label wins by
    default, because snapping back is recoverable (the next poll converges on
    whatever the label says) and honoring a phantom drag rewrites a human's
    label edit.
    """
    field = _parse_ts(field_ts)
    label = _parse_ts(label_ts)
    if field is None or label is None:
        return False
    return field > label


# --- client ------------------------------------------------------------------


class ProjectClient(Protocol):
    """The Projects/Issues surface the sync needs. Implemented by `GhClient`."""

    def option_ids(self) -> dict[str, str]: ...

    def items(self) -> list[dict]: ...

    def item_for_issue(self, number: int) -> dict | None: ...

    def last_label_event_at(self, number: int) -> str | None: ...

    def set_option(self, item_id: str, option_id: str) -> None: ...

    def clear_option(self, item_id: str) -> None: ...

    def set_status_label(
        self, number: int, add: str, remove: Sequence[str]
    ) -> None: ...

    def status_labels(self, number: int) -> list[str]: ...

    def field_option(self, item_id: str) -> str | None: ...


_FIELD_QUERY = """
query($owner: String!, $number: Int!, $field: String!) {
  user(login: $owner) {
    projectV2(number: $number) {
      id
      field(name: $field) {
        ... on ProjectV2SingleSelectField { id name options { id name } }
      }
    }
  }
}
"""

# The field VALUE's own updatedAt is the arbitration signal, so it is selected
# on the value node — not on the item.
_ITEMS_QUERY = """
query($owner: String!, $number: Int!, $after: String) {
  user(login: $owner) {
    projectV2(number: $number) {
      items(first: 50, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          type
          content {
            __typename
            ... on Issue {
              number
              state
              repository { nameWithOwner }
              labels(first: 100) {
                pageInfo { hasNextPage }
                nodes { name }
              }
            }
          }
          fieldValues(first: 100) {
            pageInfo { hasNextPage }
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                name
                updatedAt
                field { ... on ProjectV2SingleSelectField { name } }
              }
            }
          }
        }
      }
    }
  }
}
"""

_ITEM_FIELD_QUERY = """
query($item: ID!, $field: String!) {
  node(id: $item) {
    ... on ProjectV2Item {
      fieldValueByName(name: $field) {
        ... on ProjectV2ItemFieldSingleSelectValue { name }
      }
    }
  }
}
"""

_SET_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: { singleSelectOptionId: $option }
  }) { projectV2Item { id } }
}
"""

_CLEAR_MUTATION = """
mutation($project: ID!, $item: ID!, $field: ID!) {
  clearProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field
  }) { projectV2Item { id } }
}
"""


class GhClient:
    """`gh`-backed client. Needs a token with Projects v2 scope.

    In Actions that is `PROJECT_SYNC_PAT`: `secrets.GITHUB_TOKEN` cannot touch
    Projects v2 at all (the `permissions:` block has no v2 key —
    `repository-projects` is the CLASSIC boards scope), and both directions
    write Projects on every run, the mirror included.
    """

    def __init__(self, owner: str, project: int, repo: str, field: str = "Status"):
        self._owner = owner
        self._project = project
        self._repo = repo
        self._field_name = field
        self._project_id: str | None = None
        self._field_id: str | None = None
        self._options: dict[str, str] | None = None

    # -- reads
    def _gh(self, args: list[str]) -> Any:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, check=False
        )
        if proc.returncode != 0:
            raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
        return json.loads(proc.stdout or "null")

    def _load_field(self) -> None:
        if self._options is not None:
            return
        data = self._gh([
            "api", "graphql",
            "-f", f"query={_FIELD_QUERY}",
            "-F", f"owner={self._owner}",
            "-F", f"number={self._project}",
            "-f", f"field={self._field_name}",
        ])
        project = ((data.get("data") or {}).get("user") or {}).get("projectV2") or {}
        field = project.get("field") or {}
        if not field.get("id"):
            raise RuntimeError(
                f"single-select field {self._field_name!r} not found on project "
                f"{self._owner}/{self._project}"
            )
        self._project_id = project["id"]
        self._field_id = field["id"]
        self._options = {o["name"]: o["id"] for o in field.get("options") or []}

    def option_ids(self) -> dict[str, str]:
        self._load_field()
        assert self._options is not None
        return dict(self._options)

    def items(self) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            args = [
                "api", "graphql",
                "-f", f"query={_ITEMS_QUERY}",
                "-F", f"owner={self._owner}",
                "-F", f"number={self._project}",
            ]
            if cursor:
                args += ["-f", f"after={cursor}"]
            data = self._gh(args)
            page = (
                ((data.get("data") or {}).get("user") or {})
                .get("projectV2", {})
                .get("items", {})
            )
            for node in page.get("nodes") or []:
                item = self._normalize_item(node)
                # Issue numbers are only unique per repository: a user-owned
                # Project can hold foreign issues whose numbers collide with
                # this repo's, and matching by number alone would read the
                # foreign item's labels/field and relabel an unrelated local
                # issue (codex review, PR #141). Foreign items are invisible
                # to every mode.
                if (
                    item["is_issue"]
                    and item["repo"]
                    and item["repo"].lower() != self._repo.lower()
                ):
                    continue
                out.append(item)
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            cursor = info.get("endCursor")

    def _normalize_item(self, node: dict) -> dict:
        content = node.get("content") or {}
        is_issue = content.get("__typename") == "Issue"
        field_values = node.get("fieldValues") or {}
        value = None
        for fv in field_values.get("nodes") or []:
            if (fv.get("field") or {}).get("name") == self._field_name:
                value = fv
                break
        # `first: 100` exceeds Projects v2's per-project field limit, so a
        # truncated page should be impossible — but if it ever happens, an
        # absent Status must read as INDETERMINATE, not unset: treating it as
        # unset would mirror the label back over a legitimate drag (codex
        # review, PR #141).
        truncated = bool(
            value is None
            and (field_values.get("pageInfo") or {}).get("hasNextPage")
        )
        # >100 labels with the status label possibly off-page: the label
        # side is indeterminate for BOTH directions (codex review, PR #141)
        labels_truncated = bool(
            ((content.get("labels") or {}).get("pageInfo") or {}).get(
                "hasNextPage")
        )
        return {
            "item_id": node["id"],
            "is_issue": is_issue,
            "number": content.get("number") if is_issue else None,
            "repo": (content.get("repository") or {}).get("nameWithOwner")
                    if is_issue else None,
            "closed": (content.get("state") == "CLOSED") if is_issue else False,
            "labels": [
                n["name"]
                for n in ((content.get("labels") or {}).get("nodes") or [])
            ],
            "option": (value or {}).get("name"),
            "field_updated_at": (value or {}).get("updatedAt"),
            "field_page_truncated": truncated,
            "labels_truncated": labels_truncated,
        }

    def item_for_issue(self, number: int) -> dict | None:
        for item in self.items():
            if item["number"] == number:
                return item
        return None

    def last_label_event_at(self, number: int) -> str | None:
        """The LAST **status-label** `labeled`/`unlabeled` event's timestamp.

        `--paginate` and the LAST element are both load-bearing: the REST
        timeline is ascending and paginated, so a first-page read skews the
        arbitration toward "drag". Non-status label events are excluded —
        an unrelated label add must not make the label side look newer than
        a pending honored drag (codex review, PR #141).
        """
        events = self._gh([
            "api", f"repos/{self._repo}/issues/{number}/timeline",
            "--paginate", "--slurp",
        ])
        flat: list[dict] = []
        for page in events or []:
            flat.extend(page if isinstance(page, list) else [page])
        stamps = [
            e.get("created_at")
            for e in flat
            if e.get("event") in {"labeled", "unlabeled"}
            and e.get("created_at")
            and str((e.get("label") or {}).get("name", "")).startswith("status:")
        ]
        return stamps[-1] if stamps else None

    # -- writes
    def set_option(self, item_id: str, option_id: str) -> None:
        self._load_field()
        self._gh([
            "api", "graphql",
            "-f", f"query={_SET_MUTATION}",
            "-f", f"project={self._project_id}",
            "-f", f"item={item_id}",
            "-f", f"field={self._field_id}",
            "-f", f"option={option_id}",
        ])

    def clear_option(self, item_id: str) -> None:
        self._load_field()
        self._gh([
            "api", "graphql",
            "-f", f"query={_CLEAR_MUTATION}",
            "-f", f"project={self._project_id}",
            "-f", f"item={item_id}",
            "-f", f"field={self._field_id}",
        ])

    def field_option(self, item_id: str) -> str | None:
        data = self._gh([
            "api", "graphql",
            "-f", f"query={_ITEM_FIELD_QUERY}",
            "-f", f"item={item_id}",
            "-F", f"field={self._field_name}",
        ])
        node = ((data.get("data") or {}).get("node") or {})
        return ((node.get("fieldValueByName") or {}).get("name"))

    def status_labels(self, number: int) -> list[str]:
        data = self._gh(["api", f"repos/{self._repo}/issues/{number}"])
        return [
            str(label.get("name", ""))
            for label in data.get("labels") or []
            if str(label.get("name", "")).startswith("status:")
        ]

    def set_status_label(self, number: int, add: str, remove: Sequence[str]) -> None:
        args = ["issue", "edit", str(number), "--repo", self._repo, "--add-label", add]
        for label in remove:
            if label != add:
                args += ["--remove-label", label]
        subprocess.run(["gh", *args], capture_output=True, text=True, check=True)


# --- runners -----------------------------------------------------------------


def _emit(kind: str, item: dict, decision: Decision, *, dry_run: bool) -> None:
    prefix = "[dry-run] " if dry_run else ""
    print(
        f"{prefix}{kind} issue={item.get('number')} item={item.get('item_id')} "
        f"action={decision.action} reason={decision.reason}"
    )


def _apply(
    client: ProjectClient, item: dict, decision: Decision, *, dry_run: bool
) -> None:
    if dry_run or not decision.writes:
        return
    # Field-side TOCTOU guard (codex review, PR #141): every write action
    # was decided from the items() snapshot; a drag landing between snapshot
    # and write would be overwritten permanently by a stale MIRROR/SNAP_BACK
    # (and an honored-drag relabel could fire after the card moved back).
    # Require the item's live option to still match the snapshot; a mismatch
    # skips, and the next poll re-arbitrates.
    live = client.field_option(item["item_id"])
    if live != item.get("option"):
        print(
            f"skip issue={item.get('number')} field changed mid-poll "
            f"({item.get('option')!r} -> {live!r}); next poll re-arbitrates"
        )
        return
    if decision.action in {MIRROR, SNAP_BACK}:
        options = client.option_ids()
        option_id = options.get(decision.option or "")
        if option_id is None:
            raise RuntimeError(f"project has no Status option {decision.option!r}")
        client.set_option(item["item_id"], option_id)
    elif decision.action == CLEAR:
        client.clear_option(item["item_id"])
    elif decision.action == LABEL_WRITE:
        # TOCTOU guard (codex review, PR #141): the decision was computed
        # from a poll snapshot; a concurrent human relabel between snapshot
        # and write would compose with it into a dual-status state (the
        # write removes only the snapshot label). Require the snapshot's
        # status set to still hold; a mismatch skips, and the next poll
        # re-arbitrates from fresh state.
        fresh = set(client.status_labels(int(item["number"])))
        snapshot = {l for l in item["labels"] if l.startswith("status:")}
        if fresh != snapshot:
            print(
                f"skip issue={item['number']} status labels changed mid-poll "
                f"({sorted(snapshot)} -> {sorted(fresh)}); next poll re-arbitrates"
            )
            return
        client.set_status_label(
            int(item["number"]), decision.label or "", decision.remove_labels
        )


def run_mirror(
    client: ProjectClient, issue: int, *, dry_run: bool = False
) -> Decision:
    """label -> field for ONE issue (the `labeled`/`unlabeled` webhook path)."""
    item = client.item_for_issue(issue)
    if item is None:
        decision = Decision(SKIP, "issue has no project item (auto-add not yet run)")
        _emit("mirror", {"number": issue}, decision, dry_run=dry_run)
        return decision
    if item.get("labels_truncated"):
        decision = Decision(
            SKIP, "label page truncated — indeterminate, refusing to mirror"
        )
        _emit("mirror", item, decision, dry_run=dry_run)
        return decision
    decision = mirror_decision(
        item["labels"], closed=item["closed"], current_option=item["option"]
    )
    _emit("mirror", item, decision, dry_run=dry_run)
    _apply(client, item, decision, dry_run=dry_run)
    return decision


def run_poll(client: ProjectClient, *, dry_run: bool = False,
             repo: str = "") -> list[Decision]:
    """field -> label arbitration across every project item (the schedule path)."""
    allowlist = honored_drags(repo=repo)
    out: list[Decision] = []
    for item in client.items():
        if item.get("field_page_truncated") or item.get("labels_truncated"):
            decision = Decision(
                SKIP,
                "field or label page truncated — indeterminate, refusing "
                "to arbitrate from a partial snapshot",
            )
            _emit("poll", item, decision, dry_run=dry_run)
            out.append(decision)
            continue
        label_ts = (
            client.last_label_event_at(item["number"])
            if item["is_issue"] and item["number"] is not None
            else None
        )
        decision = poll_decision(
            labels=item["labels"],
            closed=item["closed"],
            is_issue=item["is_issue"],
            current_option=item["option"],
            field_updated_at=item["field_updated_at"],
            last_label_event_at=label_ts,
            allowlist=allowlist,
        )
        _emit("poll", item, decision, dry_run=dry_run)
        _apply(client, item, decision, dry_run=dry_run)
        out.append(decision)
    return out


def run_backfill(client: ProjectClient, *, dry_run: bool = False) -> list[Decision]:
    """One-shot label -> field over every item. Never honors a drag."""
    out: list[Decision] = []
    for item in client.items():
        if not item["is_issue"]:
            decision = Decision(SKIP, "item content is not an issue (PR or draft)")
        elif item.get("labels_truncated"):
            decision = Decision(
                SKIP, "label page truncated — indeterminate, refusing to mirror"
            )
        else:
            decision = mirror_decision(
                item["labels"], closed=item["closed"], current_option=item["option"]
            )
        _emit("backfill", item, decision, dry_run=dry_run)
        _apply(client, item, decision, dry_run=dry_run)
        out.append(decision)
    return out


def main(argv: Sequence[str] | None = None, *, client: ProjectClient | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m orchestrator.status_board",
        description="Sync status:* labels with a GitHub Project v2 Status field.",
    )
    parser.add_argument("--mode", choices=["mirror", "poll", "backfill"], default="poll")
    parser.add_argument("--owner", default="")
    parser.add_argument("--project", type=int, default=0)
    parser.add_argument("--repo", default="")
    parser.add_argument("--field", default="Status")
    parser.add_argument("--issue", type=int, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and log, write nothing (the local/CI-preview path)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.mode == "mirror" and args.issue is None:
        parser.error("--mode mirror requires --issue")

    if client is None:
        if not (args.owner and args.project and args.repo):
            parser.error("--owner, --project and --repo are required")
        client = GhClient(args.owner, args.project, args.repo, args.field)

    if args.mode == "mirror":
        run_mirror(client, args.issue, dry_run=args.dry_run)
    elif args.mode == "backfill":
        run_backfill(client, dry_run=args.dry_run)
    else:
        run_poll(client, dry_run=args.dry_run, repo=args.repo)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
