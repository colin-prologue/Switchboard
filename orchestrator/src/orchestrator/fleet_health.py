"""Mechanical fleet-health detection — the external observer (issue #193).

SETUP.md Stage 5b documents four degraded states and detects none of them: it
hands the operator four grep recipes and trusts them to remember to look.
launchd supervision restarts crashes; it does not *detect* degradation, and
SETUP says so outright — "**Wedged ticks — `KeepAlive` cannot detect this
one.**" This module is the one mechanical check those recipes describe.

**It is an external observer, and that is the whole design.** It runs as a
standalone process under cron or a launchd interval job, reads only local files
(the per-slug launchd log, `.run/<slug>/` markers) and `launchctl` output, and
makes no model calls and no network calls. A health signal that depends on the
monitored process being healthy reports "fine" in precisely the failure states
it exists to catch — the same argument that moved quota observation out-of-band,
and the reason `provider_circuit`'s in-memory latches are observed here through
their *symptoms in the log* rather than by asking the process anything.

Stdlib only, for the same reason: `scripts/fleet-health.sh` invokes it with a
bare `python3`, so the check still runs when the project virtualenv is the thing
that broke. It imports `.log` (stdlib-only itself) and nothing else in-tree.

Four detections, and each one is a rate or a state, never a bare count:

* **(a) crash loop** — the `[run-project] <slug> ->` startup banner repeating.
* **(b) wedged ticks** — `tick error` records growing at the tick cadence while
  the banner count does not (`scheduler.py`'s deliberate per-tick swallow, "a
  tick must never kill the service"). Two false-positive guards, both earned
  from the live log: the match is the structured **record name** anchored to the
  UTC timestamp, never a substring — a naive `grep -c 'tick error'` also matches
  handled lines like `… skipping dispatch this tick error="…"` and over-counts
  94 to 0 — and the verdict is a **rate**, because transport flakes happen
  several times a day and must read as flaky, not wedged.
* **(c) stale code** — `.run/<slug>/restart-needed.json` sitting unread (a
  healthy long-running process never relaunches, so the launch-time surfacing
  never fires), cross-checked against the `sha=` the running process stated at
  startup, which is the stronger signal: it measures the code actually loaded,
  not what the preflight last observed.
* **(d) process down** — loaded but not running is a durable silent state,
  because a clean `launchctl stop` is a successful exit and
  `KeepAlive = {SuccessfulExit: false}` will not respawn it.

**Detect and report only.** Nothing here restarts, signals, or unloads
anything; the remedy is a string in the report. The stop-then-start pair has a
documented trap (stop alone leaves the job DOWN) and an auto-remediator that got
it wrong would convert a wedged-but-alive fleet into a stopped one.

Writes exactly two files, both under `.run/` and both owned by this module: the
report (`fleet-health.json`, for the operator and for the digest ticket that
will read it) and its own observation state (`fleet-health-state.json`, the
previous counts the rates are measured against). The state is **advisory** — a
missing or corrupt file degrades to "first observation, no rate available",
never to a crash and never to a false CLEAR.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .log import log

# --- vocabulary --------------------------------------------------------------

STATE_OK = "OK"
STATE_DOWN = "DOWN"
STATE_CRASH_LOOP = "CRASH-LOOP"
STATE_WEDGED = "WEDGED"
STATE_STALE_CODE = "STALE-CODE"
STATE_UNKNOWN = "UNKNOWN"

# Declared, not inferred — the same rule the status-precedence record set for
# `status:*` labels, applied to a second state vocabulary. A slug can carry
# several findings at once and the report needs one headline state, so the order
# is committed here rather than emerging from whichever check happened to run
# first. The argument, in order:
#
#   DOWN first — nothing is running, so no claim about ticks or code age
#     describes the present. An operator who unloaded a crash-looping job wants
#     to be told it is down, not that it was looping an hour ago.
#   CRASH-LOOP above WEDGED — a process that keeps dying is not alive-but-
#     failing, and the banner growth that proves the first also suppresses the
#     second by construction (see `_wedged`).
#   WEDGED above STALE-CODE — both share the stop-then-start remedy, and the
#     one that is failing every tick right now is the more urgent sentence.
#   UNKNOWN above OK — "we could not see" must never render as "fine".
PRECEDENCE = (
    STATE_DOWN,
    STATE_CRASH_LOOP,
    STATE_WEDGED,
    STATE_STALE_CODE,
    STATE_UNKNOWN,
    STATE_OK,
)

# A degraded finding sets the exit status; a notice never does. The split is
# what keeps the check from crying wolf: a marker written twenty minutes ago,
# a transport flake, an unsupervised project — all real, none worth waking
# anybody for.
LEVEL_DEGRADED = "degraded"
LEVEL_NOTICE = "notice"

EXIT_OK = 0
EXIT_DEGRADED = 1
EXIT_USAGE = 2

REPORT_RELPATH = ".run/fleet-health.json"
STATE_RELPATH = ".run/fleet-health-state.json"
STATE_VERSION = 1

DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "switchboard"
DEFAULT_LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

JOB_PREFIX = "com.switchboard."
LAUNCHCTL_TIMEOUT_S = 10


# --- log patterns ------------------------------------------------------------
#
# Every pattern here is anchored to the start of a line, and the two structured
# ones are anchored to the UTC timestamp `log.py` writes before the record name.
# Emission sites carry a pointer back to this module, because a rename there
# silently blinds a detection here: `scripts/run-project.sh` (the banner) and
# `scheduler.py` (`orchestrator starting`, `tick error`).
#
# The anchoring is also what makes the check safe to point at its own output:
# this module's findings are logged as `fleet health` records whose evidence
# field quotes a `tick error` line, and a substring matcher would count those.

_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_TICK_ERROR_RE = re.compile(rf"^{_TS} tick error(?=$|\s)")
_STARTING_RE = re.compile(rf"^{_TS} orchestrator starting(?=$|\s)")
_PLIST_RE = re.compile(r"^com\.switchboard\.(.+)\.plist$")


def banner_pattern(slug: str) -> re.Pattern[str]:
    """The `[run-project] <slug> ->` startup banner, for one slug."""
    return re.compile(r"^\[run-project\] " + re.escape(slug) + r" ->")


def _field(line: str, name: str) -> str:
    """One `key=value` field out of a structured record; "" when absent.

    `log._fmt` quotes a value containing a space, so both shapes are read. The
    leading boundary is explicit rather than `\\b`, so `sha=` does not match
    inside `loaded_sha=`.
    """
    m = re.search(rf'(?:^|\s){re.escape(name)}=("[^"]*"|\S+)', line)
    if m is None:
        return ""
    return m.group(1).strip('"')


# --- thresholds --------------------------------------------------------------


@dataclass(frozen=True)
class Thresholds:
    """The judgment calls, in one place so they can be re-argued after the first
    false alarm. None of them has a production baseline behind it yet."""

    # SETUP's crash-loop recipe says "more than one line per intentional
    # restart". We cannot observe operator intent, so the threshold is set
    # above what any single manual stop/start could produce.
    crash_banners: int = 3

    # The scheduler's default poll interval; the denominator for "how many
    # ticks should this window have contained".
    poll_interval_s: float = 30.0

    # WEDGED needs tick errors at a rate consistent with *every* tick failing.
    # Half the expected ticks leaves room for a slow window without letting a
    # handful of ReadTimeouts read as wedged.
    wedge_ratio: float = 0.5

    # Below this many expected ticks the window says nothing about a rate.
    wedge_min_ticks: float = 2.0

    # A marker younger than this is what the launch-time surfacing already
    # showed the operator; older means it has been sitting unread.
    marker_age_hours: float = 4.0

    # Shorter than this and the baseline is not advanced — see `_window`.
    min_window_s: float = 60.0


# --- facts read off disk -----------------------------------------------------


@dataclass(frozen=True)
class LogFacts:
    """One pass over one slug's launchd log."""

    present: bool = False
    size: int = 0
    banners: int = 0
    tick_errors: int = 0
    last_banner: str = ""
    last_tick_error: str = ""
    running_sha: str = ""
    running_dirty: str = ""
    error: str = ""


def read_log(path: Path, slug: str) -> LogFacts:
    """Count the three records in one streaming pass. Never raises.

    The log has no rotation (#12) and grows without bound, so this reads line by
    line rather than slurping, and tolerates undecodable bytes — a log the check
    cannot fully decode is still a log worth counting.
    """
    banner_re = banner_pattern(slug)
    banners = tick_errors = 0
    last_banner = last_tick_error = ""
    sha = dirty = ""
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if banner_re.match(line):
                    banners += 1
                    last_banner = line
                elif _TICK_ERROR_RE.match(line):
                    tick_errors += 1
                    last_tick_error = line
                elif _STARTING_RE.match(line):
                    # Last one wins: the identity of the build currently loaded.
                    sha = _field(line, "sha")
                    dirty = _field(line, "dirty")
    except FileNotFoundError:
        return LogFacts(present=False)
    except OSError as exc:
        return LogFacts(present=False, error=str(exc))
    return LogFacts(
        present=True,
        size=size,
        banners=banners,
        tick_errors=tick_errors,
        last_banner=last_banner,
        last_tick_error=last_tick_error,
        running_sha=sha,
        running_dirty=dirty,
    )


@dataclass(frozen=True)
class Marker:
    """`.run/<slug>/restart-needed.json`, as written by the freshness preflight."""

    present: bool = False
    target_sha: str = ""
    loaded_sha: str = ""
    behind_commits: object = None
    written_at: datetime | None = None
    raw_written_at: str = ""
    error: str = ""


def read_marker(path: Path) -> Marker:
    """Read the restart-needed marker. Never raises; a marker this module cannot
    parse is reported as present-but-unreadable rather than as absent, because
    "absent" is the clean answer and an unreadable file has not earned it."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Marker(present=False)
    except OSError as exc:
        return Marker(present=True, error=str(exc))
    try:
        data = json.loads(text)
    except ValueError as exc:
        return Marker(present=True, error=f"unparseable: {exc}")
    if not isinstance(data, dict):
        return Marker(present=True, error="unparseable: not an object")
    raw = str(data.get("written_at", ""))
    return Marker(
        present=True,
        target_sha=str(data.get("target_sha", "")),
        loaded_sha=str(data.get("loaded_sha", "")),
        behind_commits=data.get("behind_commits"),
        written_at=parse_ts(raw),
        raw_written_at=raw,
    )


@dataclass(frozen=True)
class JobState:
    """What `launchctl` says about one job."""

    known: bool = False          # could we ask at all?
    loaded: bool = False
    running: bool = False
    evidence: str = ""

    @property
    def down(self) -> bool:
        return self.known and not self.running


def probe_launchctl(slug: str, *, timeout_s: int = LAUNCHCTL_TIMEOUT_S) -> JobState:
    """`launchctl list com.switchboard.<slug>`, reduced to loaded/running.

    No `launchctl` on PATH (a Linux box, a stripped environment) is `known=False`
    — the (d) detection reports that it could not look, and (b) falls back to the
    log, which is the better liveness evidence anyway.
    """
    label = f"{JOB_PREFIX}{slug}"
    if shutil.which("launchctl") is None:
        return JobState(known=False, evidence="launchctl not on PATH")
    try:
        proc = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return JobState(known=False, evidence=f"launchctl failed: {exc}")
    if proc.returncode != 0:
        return JobState(
            known=True, loaded=False, running=False,
            evidence=f"launchctl list {label} -> exit {proc.returncode}",
        )
    out = proc.stdout.decode("utf-8", "replace")
    pid = re.search(r'"PID"\s*=\s*(\d+)', out)
    if pid is not None:
        return JobState(known=True, loaded=True, running=True,
                        evidence=f"{label} loaded, PID {pid.group(1)}")
    exit_status = re.search(r'"LastExitStatus"\s*=\s*(-?\d+)', out)
    tail = f', LastExitStatus {exit_status.group(1)}' if exit_status else ""
    return JobState(known=True, loaded=True, running=False,
                    evidence=f"{label} loaded, no PID{tail}")


# --- the rate window ---------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """Growth since the previous observation, and how long that took."""

    seconds: float
    banner_growth: int
    tick_error_growth: int


@dataclass(frozen=True)
class Observation:
    """One slug's row in the state file."""

    observed_at: datetime | None
    banners: int
    tick_errors: int
    log_bytes: int
    window: Window | None


def parse_ts(raw: str) -> datetime | None:
    """UTC ISO-8601 as this repo writes it, or None. Never raises."""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_ts(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(
    prev: Observation | None,
    facts: LogFacts,
    now: datetime,
    thresholds: Thresholds,
) -> tuple[Window | None, bool, str]:
    """Return `(window, advance_baseline, note)`.

    Three cases, and the third is the idempotence guarantee:

    * **No previous observation** — first run. No rate available; record a
      baseline for next time.
    * **The counts went backwards** — the log was truncated or rotated out from
      under us (#12 owns rotation; this check only has to survive it). A naive
      delta would go negative and read as a clean fleet, so the baseline is
      discarded and this run becomes a first observation.
    * **The window is shorter than `min_window_s`** — the baseline is NOT
      advanced and the *previously measured* window is reused. Running the check
      twice back-to-back must yield the same states, and advancing the baseline
      on the first of the two runs would have made the second one report a fleet
      that had recovered in five seconds.
    """
    if prev is None or prev.observed_at is None:
        return None, True, "first observation — no rate available"
    if (facts.banners < prev.banners
            or facts.tick_errors < prev.tick_errors
            or facts.size < prev.log_bytes):
        return None, True, "log truncated or rotated — baseline reset"
    elapsed = (now - prev.observed_at).total_seconds()
    if elapsed < thresholds.min_window_s:
        return prev.window, False, ""
    return (
        Window(
            seconds=elapsed,
            banner_growth=facts.banners - prev.banners,
            tick_error_growth=facts.tick_errors - prev.tick_errors,
        ),
        True,
        "",
    )


# --- findings ----------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    state: str
    level: str
    slug: str
    evidence: str
    remedy: str

    def as_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "level": self.level,
            "evidence": self.evidence,
            "remedy": self.remedy,
        }


@dataclass(frozen=True)
class SlugResult:
    slug: str
    state: str
    findings: tuple[Finding, ...]
    observation: Observation
    notes: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return any(f.level == LEVEL_DEGRADED for f in self.findings)


def _log_path(log_dir: Path, slug: str) -> Path:
    return log_dir / f"{slug}.log"


def _plist_path(launch_agents_dir: Path, slug: str) -> Path:
    return launch_agents_dir / f"{JOB_PREFIX}{slug}.plist"


def _stop_then_start(slug: str) -> str:
    return (
        f"launchctl stop {JOB_PREFIX}{slug} THEN launchctl start "
        f"{JOB_PREFIX}{slug} — stop alone leaves it DOWN "
        "(KeepAlive SuccessfulExit:false; SETUP.md Stage 5b)"
    )


def _down(slug: str, job: JobState, plist: Path) -> list[Finding]:
    """(d) — the expectation is an installed plist; the observation is launchctl."""
    if not plist.is_file():
        return []
    if not job.known:
        return [Finding(
            STATE_UNKNOWN, LEVEL_NOTICE, slug,
            f"{plist} installed but launchd could not be queried ({job.evidence})",
            f"run the check on the machine that supervises '{slug}'",
        )]
    if not job.loaded:
        return [Finding(
            STATE_DOWN, LEVEL_DEGRADED, slug,
            f"{plist} installed but the job is not loaded ({job.evidence})",
            f"launchctl load {plist}",
        )]
    if not job.running:
        return [Finding(
            STATE_DOWN, LEVEL_DEGRADED, slug,
            f"{job.evidence} — a clean `launchctl stop` is a successful exit, "
            "so KeepAlive will not respawn it",
            f"launchctl start {JOB_PREFIX}{slug}",
        )]
    return []


def _crash_loop(
    slug: str, facts: LogFacts, window: Window | None, thresholds: Thresholds,
) -> list[Finding]:
    """(a) — banner growth, not a bare banner count."""
    if window is None or window.banner_growth < thresholds.crash_banners:
        return []
    return [Finding(
        STATE_CRASH_LOOP, LEVEL_DEGRADED, slug,
        f"{window.banner_growth} new startup banners in "
        f"{int(window.seconds)}s; last: {facts.last_banner}",
        f"read the traceback following the banner in the log; "
        f"launchctl unload ~/Library/LaunchAgents/{JOB_PREFIX}{slug}.plist "
        "to stop the loop",
    )]


def _wedged(
    slug: str,
    facts: LogFacts,
    window: Window | None,
    job: JobState,
    thresholds: Thresholds,
) -> list[Finding]:
    """(b) — alive, and failing every tick.

    Gated on the process not being known-DOWN: a stopped job's log cannot grow,
    so a rate computed across a stop is measuring the window before it.
    """
    if window is None or window.tick_error_growth <= 0 or job.down:
        return []
    if window.banner_growth > 0:
        # It restarted inside the window; the tick errors may predate the
        # restart, and (a) already owns the louder story.
        return []
    expected_ticks = window.seconds / thresholds.poll_interval_s
    wedged = (
        expected_ticks >= thresholds.wedge_min_ticks
        and window.tick_error_growth >= thresholds.wedge_ratio * expected_ticks
    )
    evidence = (
        f"{window.tick_error_growth} new tick-error records in "
        f"{int(window.seconds)}s (~{expected_ticks:.0f} ticks expected), "
        f"no new startup banner; last: {facts.last_tick_error}"
    )
    if not wedged:
        return [Finding(
            STATE_OK, LEVEL_NOTICE, slug,
            evidence + " — below the wedge rate; reads as transient flakiness",
            "no action; watch it if the rate climbs",
        )]
    return [Finding(
        STATE_WEDGED, LEVEL_DEGRADED, slug, evidence, _stop_then_start(slug))]


def _stale_code(
    slug: str, facts: LogFacts, marker: Marker, now: datetime,
    thresholds: Thresholds,
) -> list[Finding]:
    """(c) — marker age as the base signal, startup identity as the direct one."""
    if not marker.present:
        return []
    if marker.error:
        return [Finding(
            STATE_UNKNOWN, LEVEL_NOTICE, slug,
            f"restart-needed marker present but unreadable ({marker.error})",
            "inspect .run/{}/restart-needed.json".format(slug),
        )]

    evidence = (
        f"restart-needed marker: behind_commits={marker.behind_commits} "
        f"loaded_sha={marker.loaded_sha[:12] or '?'} "
        f"target_sha={marker.target_sha[:12] or '?'} "
        f"written_at={marker.raw_written_at or '?'}"
    )

    # The direct signal, available since the startup record began carrying
    # `sha=`: what the process actually loaded beats what the preflight last
    # observed. `sha=unknown` (no git, an exported copy) degrades to marker age.
    running = facts.running_sha
    if running and running != "unknown" and marker.target_sha:
        if running == marker.target_sha:
            return [Finding(
                STATE_OK, LEVEL_NOTICE, slug,
                evidence + f" — but the running process states sha={running[:12]}, "
                "which matches the target: the marker is superseded",
                "none; the marker will clear at the next preflight",
            )]
        return [Finding(
            STATE_STALE_CODE, LEVEL_DEGRADED, slug,
            evidence + f" — the running process states sha={running[:12]}"
            + (f" dirty={facts.running_dirty}" if facts.running_dirty else ""),
            _stop_then_start(slug),
        )]

    if marker.written_at is None:
        return [Finding(
            STATE_UNKNOWN, LEVEL_NOTICE, slug,
            evidence + " — written_at is unparseable, so marker age is unavailable",
            "inspect .run/{}/restart-needed.json".format(slug),
        )]
    age_hours = (now - marker.written_at).total_seconds() / 3600.0
    if age_hours < thresholds.marker_age_hours:
        return [Finding(
            STATE_OK, LEVEL_NOTICE, slug,
            evidence + f" — {age_hours:.1f}h old; the launch preflight already "
            "surfaced this",
            "none yet; it becomes a finding if it is still here in "
            f"{thresholds.marker_age_hours:g}h",
        )]
    return [Finding(
        STATE_STALE_CODE, LEVEL_DEGRADED, slug,
        evidence + f" — {age_hours:.1f}h old and still unread",
        _stop_then_start(slug),
    )]


def _headline(findings: tuple[Finding, ...]) -> str:
    """The slug's one state, by declared precedence."""
    present = {f.state for f in findings if f.state != STATE_OK}
    for state in PRECEDENCE:
        if state in present:
            return state
    return STATE_OK


# --- the check ---------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    repo_root: Path
    log_dir: Path
    launch_agents_dir: Path

    @property
    def report(self) -> Path:
        return self.repo_root / REPORT_RELPATH

    @property
    def state(self) -> Path:
        return self.repo_root / STATE_RELPATH


def discover_slugs(paths: Paths) -> list[str]:
    """Every slug the machine knows about, locally and without a tracker query:
    registered project bindings plus installed LaunchAgents. Their union, because
    a plist with no binding and a binding with no plist are both worth naming."""
    slugs = {
        env.parent.name
        for env in (paths.repo_root / "projects").glob("*/project.env")
    }
    for plist in paths.launch_agents_dir.glob(f"{JOB_PREFIX}*.plist"):
        m = _PLIST_RE.match(plist.name)
        if m:
            slugs.add(m.group(1))
    return sorted(slugs)


def check_slug(
    slug: str,
    paths: Paths,
    thresholds: Thresholds,
    prev: Observation | None,
    now: datetime,
    job: JobState,
) -> SlugResult:
    """Everything this check can say about one slug. Pure with respect to
    `launchctl` — the job state is passed in, so the whole detection surface is
    testable without a supervisor."""
    log_path = _log_path(paths.log_dir, slug)
    plist = _plist_path(paths.launch_agents_dir, slug)
    facts = read_log(log_path, slug)
    marker = read_marker(paths.repo_root / ".run" / slug / "restart-needed.json")

    window, advance, note = _window(prev, facts, now, thresholds)
    notes = [note] if note else []

    findings: list[Finding] = []
    findings += _down(slug, job, plist)
    findings += _crash_loop(slug, facts, window, thresholds)
    findings += _wedged(slug, facts, window, job, thresholds)
    findings += _stale_code(slug, facts, marker, now, thresholds)

    if not facts.present:
        detail = f" ({facts.error})" if facts.error else ""
        findings.append(Finding(
            STATE_UNKNOWN, LEVEL_NOTICE, slug,
            f"no log at {log_path}{detail}"
            + ("" if plist.is_file() else " and no LaunchAgent installed"),
            "install the LaunchAgent (SETUP.md Stage 5b) if this project is "
            "meant to be supervised; ignore if it runs in the foreground",
        ))

    observation = Observation(
        observed_at=now if advance else (prev.observed_at if prev else now),
        banners=facts.banners if advance else (prev.banners if prev else facts.banners),
        tick_errors=(
            facts.tick_errors if advance
            else (prev.tick_errors if prev else facts.tick_errors)),
        log_bytes=facts.size if advance else (prev.log_bytes if prev else facts.size),
        window=window,
    )
    return SlugResult(
        slug=slug,
        state=_headline(tuple(findings)),
        findings=tuple(findings),
        observation=observation,
        notes=tuple(notes),
    )


def check_fleet(
    slugs: list[str],
    paths: Paths,
    thresholds: Thresholds,
    state: dict[str, Observation],
    now: datetime,
    probe=None,
) -> list[SlugResult]:
    # Resolved at call time, not bound as a default: a default argument would
    # capture the function object at import and silently outlive any later
    # substitution of it.
    ask = probe_launchctl if probe is None else probe
    return [
        check_slug(slug, paths, thresholds, state.get(slug), now, ask(slug))
        for slug in slugs
    ]


# --- state and report files --------------------------------------------------


def load_state(path: Path) -> dict[str, Observation]:
    """Previous observations, or `{}`. Never raises — the state is advisory, and
    a corrupt file must degrade to "no rate available", never to a crash."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        return {}
    rows = data.get("slugs")
    if not isinstance(rows, dict):
        return {}
    out: dict[str, Observation] = {}
    for slug, row in rows.items():
        if not isinstance(row, dict):
            continue
        raw_window = row.get("window")
        window = None
        if isinstance(raw_window, dict):
            try:
                window = Window(
                    seconds=float(raw_window["seconds"]),
                    banner_growth=int(raw_window["banner_growth"]),
                    tick_error_growth=int(raw_window["tick_error_growth"]),
                )
            except (KeyError, TypeError, ValueError):
                window = None
        try:
            out[str(slug)] = Observation(
                observed_at=parse_ts(str(row.get("observed_at", ""))),
                banners=int(row.get("banners", 0)),
                tick_errors=int(row.get("tick_errors", 0)),
                log_bytes=int(row.get("log_bytes", 0)),
                window=window,
            )
        except (TypeError, ValueError):
            continue
    return out


def _observation_dict(obs: Observation) -> dict[str, object]:
    row: dict[str, object] = {
        "observed_at": format_ts(obs.observed_at) if obs.observed_at else "",
        "banners": obs.banners,
        "tick_errors": obs.tick_errors,
        "log_bytes": obs.log_bytes,
        "window": None,
    }
    if obs.window is not None:
        row["window"] = {
            "seconds": round(obs.window.seconds, 3),
            "banner_growth": obs.window.banner_growth,
            "tick_error_growth": obs.window.tick_error_growth,
        }
    return row


def build_report(results: list[SlugResult], now: datetime) -> dict[str, object]:
    return {
        "generated_at": format_ts(now),
        "degraded": any(r.degraded for r in results),
        "degraded_slugs": sorted(r.slug for r in results if r.degraded),
        "slugs": {
            r.slug: {
                "state": r.state,
                "degraded": r.degraded,
                "findings": [f.as_dict() for f in r.findings],
                "notes": list(r.notes),
                "observation": _observation_dict(r.observation),
            }
            for r in results
        },
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomic-ish write into `.run/`. The only mutation this module performs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# --- CLI ---------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleet-health",
        description="Mechanical fleet-health check: crash loops, wedged ticks, "
                    "stale loaded code, down processes. Reads local files only.",
    )
    p.add_argument("slugs", nargs="*", metavar="slug",
                   help="slugs to check (default: every registered/installed slug)")
    p.add_argument("--repo-root", type=Path, default=None,
                   help="Switchboard checkout (default: this file's repo)")
    p.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    p.add_argument("--launch-agents-dir", type=Path,
                   default=DEFAULT_LAUNCH_AGENTS_DIR)
    p.add_argument("--json", action="store_true",
                   help="print the report to stdout as well as writing it")
    p.add_argument("--crash-banners", type=int, default=Thresholds.crash_banners)
    p.add_argument("--poll-interval-s", type=float,
                   default=Thresholds.poll_interval_s)
    p.add_argument("--wedge-ratio", type=float, default=Thresholds.wedge_ratio)
    p.add_argument("--marker-age-hours", type=float,
                   default=Thresholds.marker_age_hours)
    p.add_argument("--min-window-s", type=float, default=Thresholds.min_window_s)
    return p


def _default_repo_root() -> Path:
    # orchestrator/src/orchestrator/fleet_health.py -> repo root
    return Path(__file__).resolve().parents[3]


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = Paths(
        repo_root=(args.repo_root or _default_repo_root()).resolve(),
        log_dir=args.log_dir.expanduser(),
        launch_agents_dir=args.launch_agents_dir.expanduser(),
    )
    thresholds = Thresholds(
        crash_banners=args.crash_banners,
        poll_interval_s=args.poll_interval_s,
        wedge_ratio=args.wedge_ratio,
        marker_age_hours=args.marker_age_hours,
        min_window_s=args.min_window_s,
    )
    slugs = args.slugs or discover_slugs(paths)
    if not slugs:
        log("fleet health", state=STATE_UNKNOWN,
            evidence=f"no registered projects under {paths.repo_root}/projects "
                     f"and no LaunchAgents under {paths.launch_agents_dir}",
            remedy="register a project (SETUP.md Stage 6) or pass a slug")
        return EXIT_OK

    now = datetime.now(timezone.utc)
    state = load_state(paths.state)
    results = check_fleet(slugs, paths, thresholds, state, now)

    for result in results:
        for note in result.notes:
            log("fleet health", state=result.state, slug=result.slug, note=note)
        if not result.findings:
            log("fleet health", state=STATE_OK, slug=result.slug)
        for finding in result.findings:
            log("fleet health", state=finding.state, level=finding.level,
                slug=result.slug, evidence=finding.evidence, remedy=finding.remedy)

    report = build_report(results, now)
    state.update({r.slug: r.observation for r in results})
    try:
        _write_json(paths.report, report)
        _write_json(paths.state, {
            "version": STATE_VERSION,
            "slugs": {s: _observation_dict(o) for s, o in state.items()},
        })
    except OSError as exc:
        # A report we could not persist is still a report we printed. The
        # findings above are the operator-facing surface; the file is for the
        # digest that will read it later.
        log("fleet health report write failed", error=str(exc))

    degraded = sorted(r.slug for r in results if r.degraded)
    log("fleet health report", checked=len(results), degraded=len(degraded),
        slugs=",".join(degraded) or None, path=str(paths.report))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return EXIT_DEGRADED if degraded else EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main(sys.argv[1:]))
