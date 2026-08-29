"""Audit for shipped-but-unwired features (issue #172).

Three features shipped green, reviewed and merged — and never executed, each
found by tripping over it rather than by any check. The failure is structural:
**tests exercise the feature, not its enablement.** A unit test constructs a
config with `bot_logins` populated and asserts the loop behaves; nothing
asserts that any *real* project ever populates it, because enabling is a human
act outside the test surface.

So this module reports, for one project, every config field sitting at its
documented disabling default — the switch that was never flipped.

**It reads the COMPOSED config, never the tracked template.** That distinction
is itself a failure mode (`freshness-preflight.sh` recomposes from `origin`
into `.run/<slug>/composed-WORKFLOW.md`, which is the file the orchestrator
actually loads), so a check reading `projects/<slug>/WORKFLOW.md` would report
on bytes nobody runs.

**Table-driven, not inferred.** Only fields listed in
`workflow/disabling-defaults.yml` are in scope; nothing here scans for
empty-looking values. A disabling default is a documented property of a field
(`fold.operator_logins: []` short-circuits the sub-poll), not a shape.

**Deliberately-off is a per-project assertion, never template prose.**
`workflow/WORKFLOW.base.md` describes `review_response.bot_logins` as "SHIPPED
EMPTY ON PURPOSE" — that sentence documents the *template's* default, the value
every project starts from, and is explicitly not an exemption for any project.
Only a `deliberately_off:` entry keyed by project slug silences the check.
Without that rule the headline instance would be simultaneously the thing the
check must report and the thing it must ignore.

**Stdlib only, on purpose.** `scripts/freshness-preflight.sh` invokes this with
a bare `python3` at launch, before any project virtualenv is guaranteed to
exist. Importing `yaml` here would make the common failure "the audit silently
never ran" — which is the exact bug class this module exists to end. The cost
is the small, strict value reader below: it understands scalars, flow
sequences and block sequences, and it refuses (loudly) anything else rather
than guessing. It never parses the whole front matter, because the real one
carries block scalars under `hooks:` that no subset reader should try to hold.

Consumers: `scripts/freshness-preflight.sh` (advisory stderr warnings) and
`orchestrator/tests/test_disabling_defaults.py`. Findings never gate a launch.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TABLE_RELPATH = "workflow/disabling-defaults.yml"

# The field is not present in the composed config at all.
MISSING = object()
# The field is present but holds a nested map — a shape this reader does not
# compare. Not equal to any disabling default, so it is never reported.
COMPLEX = object()

REASON_UNSET = "unset"
REASON_DEFAULT = "disabling-default"


class TableError(Exception):
    """`workflow/disabling-defaults.yml` is missing or not understandable."""


class AuditError(Exception):
    """The composed workflow could not be read."""


@dataclass(frozen=True)
class Finding:
    """One field of one project sitting in its switched-off position."""

    slug: str
    field: str
    reason: str
    disabling_value: Any

    @property
    def detail(self) -> str:
        if self.reason == REASON_UNSET:
            return (
                f"{self.field} carries no value in the composed config "
                "— the feature is shipped but not enabled"
            )
        return (
            f"{self.field} is at its disabling default "
            f"({_render(self.disabling_value)}) "
            "— the feature is shipped but not enabled"
        )

    def __str__(self) -> str:
        return f"unwired feature in '{self.slug}': {self.detail}"


@dataclass(frozen=True)
class Table:
    """The declared policy: what "off" means, and who means it."""

    defaults: dict[str, Any]
    deliberately_off: dict[str, tuple[str, ...]]


# --- the small strict value reader -------------------------------------------

def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _strip_comment(text: str) -> str:
    """Drop a trailing `#` comment, honouring quotes (`a#b` is not a comment)."""
    out: list[str] = []
    quote: str | None = None
    prev_space = True
    for ch in text:
        if quote is not None:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            prev_space = False
            continue
        if ch == "#" and prev_space:
            break
        out.append(ch)
        prev_space = ch in " \t"
    return "".join(out)


def _significant(line: str) -> str | None:
    """The line's content with comments stripped, or None if it carries none."""
    text = _strip_comment(line).strip()
    return text or None


def _split_flow(inner: str) -> list[str]:
    """Split a flow-sequence body on commas that are not inside quotes."""
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _scalar(text: str) -> Any:
    """Normalize one scalar or flow value into a comparable Python value.

    Both sides of every comparison go through here, so `[]` in the table and
    `[  ]` in a composed file are the same value, and so are `"never"` and
    `never`. Nested flow collections are deliberately unsupported: no disabling
    default needs one, and a reader that guessed at them would be the fake
    fidelity this project keeps finding.
    """
    t = text.strip()
    if t in ("", "null", "~"):
        return None
    if t == "{}":
        return {}
    if t.startswith("[") and t.endswith("]"):
        inner = t[1:-1].strip()
        if "[" in inner or "]" in inner:
            raise TableError(f"nested flow collections are not supported: {t!r}")
        return tuple(_scalar(part) for part in _split_flow(inner))
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    if t in ("true", "True"):
        return True
    if t in ("false", "False"):
        return False
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        pass
    return t


def _render(value: Any) -> str:
    if isinstance(value, tuple):
        return "[" + ", ".join(_render(v) for v in value) + "]"
    if isinstance(value, str):
        return f'"{value}"'
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _block_end(lines: list[str], start: int, end: int, indent: int) -> int:
    """First index at or after `start` whose line is significant and not deeper."""
    for j in range(start, end):
        if _significant(lines[j]) is not None and _indent(lines[j]) <= indent:
            return j
    return end


def _child_indent(lines: list[str], start: int, end: int) -> int | None:
    for j in range(start, end):
        if _significant(lines[j]) is not None:
            return _indent(lines[j])
    return None


def _sequence(lines: list[str], start: int, end: int, indent: int) -> tuple | None:
    """The block sequence in this window, or None if it is not one."""
    items: list[Any] = []
    for j in range(start, end):
        text = _significant(lines[j])
        if text is None:
            continue
        if _indent(lines[j]) != indent:
            continue
        if not text.startswith("-"):
            return None
        items.append(_scalar(text[1:]))
    return tuple(items)


# --- reading the composed workflow -------------------------------------------

def front_matter(text: str) -> str:
    """The YAML front matter of a WORKFLOW.md-style file, as raw lines.

    Mirrors `workflow.load_workflow`'s split rather than re-deriving it: a
    leading `---` opens the block and the next bare `---` closes it. A file
    without front matter yields nothing to audit.
    """
    if not text.startswith("---"):
        return ""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        out.append(line)
    return "\n".join(out)


def field_value(front: str, path: str) -> Any:
    """The value at a dotted `path`, or `MISSING` / `COMPLEX`.

    Targeted rather than a full parse: only the lines on the path are read, so
    a block scalar somewhere else in the front matter (`hooks.after_create`)
    cannot make the audit fail open.
    """
    segments = path.split(".")
    lines = front.splitlines()
    start, end, indent = 0, len(lines), 0

    for depth, segment in enumerate(segments):
        found = None
        for i in range(start, end):
            text = _significant(lines[i])
            if text is None:
                continue
            cur = _indent(lines[i])
            if cur < indent:
                break
            if cur > indent:
                continue
            key, sep, rest = text.partition(":")
            if not sep or key.strip() != segment:
                continue
            found, inline = i, rest.strip()
            break
        if found is None:
            return MISSING

        stop = _block_end(lines, found + 1, end, indent)
        if depth < len(segments) - 1:
            child = _child_indent(lines, found + 1, stop)
            if child is None or inline:
                return MISSING
            start, end, indent = found + 1, stop, child
            continue

        if inline:
            return _scalar(inline)
        child = _child_indent(lines, found + 1, stop)
        if child is None:
            return None
        seq = _sequence(lines, found + 1, stop, child)
        return COMPLEX if seq is None else seq

    return MISSING


# --- reading the policy table ------------------------------------------------

def _parse_map(lines: list[str], start: int, end: int, indent: int) -> dict:
    out: dict[str, Any] = {}
    i = start
    while i < end:
        text = _significant(lines[i])
        if text is None:
            i += 1
            continue
        cur = _indent(lines[i])
        if cur < indent:
            break
        if cur > indent:
            raise TableError(
                f"unexpected indentation at line {i + 1}: {lines[i]!r}"
            )
        key, sep, rest = text.partition(":")
        if not sep:
            raise TableError(f"expected 'key: value' at line {i + 1}: {lines[i]!r}")
        key = key.strip()
        if key in out:
            raise TableError(f"duplicate key {key!r} at line {i + 1}")
        stop = _block_end(lines, i + 1, end, cur)
        inline = rest.strip()
        if inline:
            out[key] = _scalar(inline)
        else:
            child = _child_indent(lines, i + 1, stop)
            if child is None:
                out[key] = None
            else:
                seq = _sequence(lines, i + 1, stop, child)
                out[key] = (
                    _parse_map(lines, i + 1, stop, child) if seq is None else seq
                )
        i = stop
    return out


def parse_table(text: str) -> Table:
    """Parse `workflow/disabling-defaults.yml`.

    Strict by design: anything this reader does not understand raises rather
    than parsing to an empty policy, because an empty policy reports nothing
    and looks exactly like a clean project.
    """
    raw = _parse_map(text.splitlines(), 0, len(text.splitlines()), 0)
    unknown = sorted(set(raw) - {"defaults", "deliberately_off"})
    if unknown:
        raise TableError("unknown top-level keys: " + ", ".join(unknown))

    defaults_raw = raw.get("defaults") or {}
    if not isinstance(defaults_raw, dict):
        raise TableError("`defaults` must be a map of field path -> off value")
    for field in defaults_raw:
        if not field or field.startswith(".") or field.endswith("."):
            raise TableError(f"invalid field path {field!r} under `defaults`")

    off_raw = raw.get("deliberately_off") or {}
    if not isinstance(off_raw, dict):
        raise TableError("`deliberately_off` must be a map of slug -> field paths")
    exemptions: dict[str, tuple[str, ...]] = {}
    for slug, fields in off_raw.items():
        if fields is None:
            exemptions[slug] = ()
            continue
        if not isinstance(fields, tuple):
            raise TableError(
                f"deliberately_off.{slug} must be a list of field paths"
            )
        exemptions[slug] = tuple(str(f) for f in fields)

    return Table(defaults=dict(defaults_raw), deliberately_off=exemptions)


def default_table_path() -> Path:
    """`workflow/disabling-defaults.yml` at the repo root above this package."""
    return Path(__file__).resolve().parents[3] / TABLE_RELPATH


def load_table(path: Path | str | None = None) -> Table:
    table_path = Path(path) if path is not None else default_table_path()
    try:
        text = table_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TableError(f"cannot read {table_path}: {exc}") from exc
    return parse_table(text)


# --- the check ---------------------------------------------------------------

def audit_composed_workflow(
    slug: str,
    composed_path: Path | str,
    table: Table | Path | str | None = None,
) -> list[Finding]:
    """Every declared field sitting at its disabling default for `slug`.

    `composed_path` is `$SB_HOME/.run/<slug>/composed-WORKFLOW.md` — the bytes
    the orchestrator loads. Findings come back in table-declaration order, and
    fields this project has declared deliberately off are absent from them.
    """
    policy = table if isinstance(table, Table) else load_table(table)

    path = Path(composed_path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"cannot read composed workflow {path}: {exc}") from exc

    front = front_matter(text)
    exempt = set(policy.deliberately_off.get(slug, ()))

    findings: list[Finding] = []
    for field, off_value in policy.defaults.items():
        if field in exempt:
            continue
        observed = field_value(front, field)
        if observed is COMPLEX:
            continue
        if observed is MISSING or (observed is None and off_value is not None):
            findings.append(Finding(slug, field, REASON_UNSET, off_value))
        elif observed == off_value and type(observed) is type(off_value):
            findings.append(Finding(slug, field, REASON_DEFAULT, off_value))
    return findings


# --- CLI (freshness-preflight.sh) --------------------------------------------

def main(argv: list[str]) -> int:
    """Print one advisory line per finding. Always fail-open, never a gate.

    Errors go to stderr as one line the caller can warn verbatim; the exit
    status stays 0 for everything except a usage error, so a caller running
    under `set -e` cannot be turned into a launch refusal by this audit.
    """
    if len(argv) < 2:
        print(
            "usage: python -m orchestrator.disabling_defaults "
            "<slug> <composed-workflow> [<table>]",
            file=sys.stderr,
        )
        return 2
    slug, composed = argv[0], argv[1]
    table_path = argv[2] if len(argv) > 2 else None
    try:
        findings = audit_composed_workflow(slug, composed, table_path)
    except (TableError, AuditError) as exc:
        print(f"unwired audit skipped for '{slug}': {exc}", file=sys.stderr)
        return 0
    for finding in findings:
        print(str(finding))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main(sys.argv[1:]))
