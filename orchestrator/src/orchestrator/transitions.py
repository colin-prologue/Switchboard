"""Status-transition table access (issue #29, part A).

The lifecycle rules live in ONE committed YAML file, `workflow/transitions.yml`,
at the repo root — never duplicated as a Python literal (that is the drift the
single-path-constant AC guards against). The orchestrator consumes the two
sections answerable from a CURRENT label set alone: `requires_marker` (per-state
provenance markers it checks against the state + labels it observes) and
`precedence` (which state wins when an issue carries more than one `status:*`
label — issue #167). The `edges` section is for #52's Action, which sees both
transition endpoints in its event payload.

See self/.decisions/AgDR-011-* for why the marker check is a bounded, config-
driven exception to AgDR-006 ("gates cost zero orchestrator code").
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .types import WorkflowError

# Single committed path constant. The package lives at
# orchestrator/src/orchestrator/, so parents[3] is the repo root that also holds
# workflow/. This mirrors how the prompt tests locate workflow/WORKFLOW.base.md.
TRANSITIONS_PATH = Path(__file__).resolve().parents[3] / "workflow" / "transitions.yml"


def load_edges(path: Path = TRANSITIONS_PATH) -> list[dict]:
    """Load the `edges` section verbatim: the full (from -> to) rule set.

    Issue #22's status-board sync is the FIRST consumer of this section (the
    header's "#52's labeled-event Action" does not exist at HEAD). Rows are
    returned as authored — `from`/`to` keep the dashed spelling the file uses;
    callers normalize before comparing against tracker state strings, which use
    spaces. Non-map rows are dropped rather than crashing the Action.
    """
    raw = _load_table(path)
    section = raw.get("edges") or []
    if not isinstance(section, list):
        raise WorkflowError(
            "transitions_parse_error", "edges must be a list of (from, to) maps"
        )
    return [e for e in section if isinstance(e, dict) and "from" in e and "to" in e]


def _load_table(path: Path) -> dict:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkflowError("missing_transitions_file", str(exc)) from exc
    except yaml.YAMLError as exc:
        raise WorkflowError("transitions_parse_error", str(exc)) from exc

    if not isinstance(raw, dict):
        raise WorkflowError(
            "transitions_parse_error",
            f"transitions.yml decoded to {type(raw).__name__}, expected a map",
        )
    return raw


def load_precedence(path: Path = TRANSITIONS_PATH) -> tuple[str, ...]:
    """Load the `precedence` section: states highest-priority FIRST (issue #167).

    Returned in the tracker's state vocabulary (lower-case, spaces not dashes)
    the way `load_requires_marker` returns its keys, so a caller ranking derived
    states never has to remember which spelling this file uses. Duplicates keep
    their first (highest) position; blank and non-string rows are dropped.

    Raises rather than falling back to an implicit order: an absent or malformed
    section would silently restore the alphabetical coincidence this section was
    written to retire, and a parked issue would derive an ACTIVE state again.
    """
    raw = _load_table(path)

    section = raw.get("precedence")
    if not isinstance(section, list) or not section:
        raise WorkflowError(
            "transitions_parse_error",
            "precedence must be a non-empty list of states, highest first",
        )

    out: list[str] = []
    for state in section:
        if not isinstance(state, str):
            continue
        norm = state.strip().lower().replace("-", " ")
        if norm and norm not in out:
            out.append(norm)
    if not out:
        raise WorkflowError(
            "transitions_parse_error", "precedence list contains no usable states"
        )
    return tuple(out)


def load_requires_marker(path: Path = TRANSITIONS_PATH) -> dict[str, list[str]]:
    """Load the `requires_marker` section: state (normalized) -> required markers.

    Returns a mapping whose keys are normalized states (lower-case, spaces not
    dashes — matching tracker.py state derivation) and whose values are the list
    of provenance labels an issue in that state must carry to be dispatchable.
    Absent/empty section -> empty mapping (no state gated).
    """
    raw = _load_table(path)

    section = raw.get("requires_marker") or {}
    if not isinstance(section, dict):
        raise WorkflowError(
            "transitions_parse_error",
            "requires_marker must be a map of state -> [markers]",
        )

    out: dict[str, list[str]] = {}
    for state, markers in section.items():
        if not isinstance(state, str):
            continue
        if isinstance(markers, str):
            markers = [markers]
        if not isinstance(markers, list):
            continue
        norm = [m.strip().lower() for m in markers if isinstance(m, str) and m.strip()]
        if norm:
            out[state.strip().lower()] = norm
    return out
