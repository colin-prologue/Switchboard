"""Tests for scripts/new-ticket.sh.

The worker allowlist only permits `uv run --project orchestrator ... pytest`, so the
script is never invoked directly on the command line — it is exercised here via
subprocess in its two network-free modes (--scaffold and --dry-run). These assert
flag->payload mapping and body-skeleton section presence; real filing (gh writes)
is out of scope for the harness.

implements: issue #18 (executable ticket-creation pathway)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "new-ticket.sh"


def run(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


# --- existence / executability -----------------------------------------------


def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), "scripts/new-ticket.sh must be executable"


# --- scaffold ----------------------------------------------------------------


def test_scaffold_emits_all_sections_and_exits_clean() -> None:
    proc = run("--scaffold")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for section in (
        "## In brief",
        "## Intent",
        "## Acceptance criteria",
        "## Non-goals",
        "## Consumers of mutated state",
        "## Assumptions",
    ):
        assert section in out, f"scaffold missing section: {section}"


def test_scaffold_pins_drafting_quality_content() -> None:
    # Issue #14's recurring failure classes are encoded at the drafting altitude:
    # the consumers section is ALWAYS emitted (deletion is the author's explicit
    # act), and the citation rule rides under Assumptions.
    proc = run("--scaffold")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # Consumers-of-mutated-state: always emitted, with the delete-guard comment.
    assert "## Consumers of mutated state" in out
    assert (
        "<!-- delete this section only if the ticket writes NO shared state:"
        " labels, issue state, workspaces, env -->" in out
    )
    # Citation rule (claim-vs-code drift) lives under Assumptions.
    assert (
        "Every cited mechanism carries a `file:line` verified at a named HEAD sha;"
        " uncitable claims are labeled guesses." in out
    )


def test_scaffold_leads_with_in_brief_block() -> None:
    # The plain-language layer (spec 2026-08-08) sits ABOVE the citation-dense
    # body, not instead of it. Two rules make the fields hard to pad, and both
    # must reach the author inside the skeleton itself: the identifier ban on
    # the first field, and the if/then consequence shape on the second.
    proc = run("--scaffold")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.lstrip().startswith("## In brief"), (
        "## In brief must be the FIRST section of the skeleton, above ## Intent"
    )
    assert "**What this does:**" in out
    assert "**What could be wrong:**" in out
    # Rule 1 — the identifier ban.
    assert "no issue numbers, file paths, AgDR ids" in out
    # Rule 2 — the conditional-and-consequence shape.
    assert '"if X, then Y"' in out
    # The dense body survives underneath, in order.
    assert out.index("## In brief") < out.index("## Intent")


# --- dry-run: flag -> payload mapping ----------------------------------------


def test_dry_run_maps_all_flags_to_payload() -> None:
    proc = run(
        "--dry-run",
        "--title", "Fix the thing",
        "--repo", "owner/name",
        "--entry", "todo",
        "--milestone", "Sprint 3",
        "--blocked-by", "12, 34,56",
        stdin="hello body\nsecond line\n",
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert "repo:       owner/name" in out
    assert "title:      Fix the thing" in out
    # --entry todo self-stamps the triage-PASS marker (issue #29): direct-entry
    # todos must be dispatchable, and the dispatch guard refuses unstamped ones.
    assert "labels:     status:todo,gate:triage-passed" in out
    assert "milestone:  Sprint 3" in out
    assert "blocked-by: 12 34 56" in out              # parsed & normalized
    assert "hello body" in out                        # body from stdin
    assert "second line" in out


# --- entry resolution: the target project's active_states (issue #176) --------
#
# `--entry` has no fixed default any more. A fixed one composed with the
# `register-project.sh` stance default into a `status:triage` ticket on a
# project that never dispatches triage — never picked up, and silently so. The
# default is now READ from the project the repo is bound to.
#
# Fake fidelity: the fixture projects carry the REAL stance templates as their
# composed WORKFLOW.md, not a hand-written `active_states` line. A fixture that
# restated the state lists would keep passing the day a stance changes them.


def _project(sb_home: Path, slug: str, repo: str, template: Path) -> Path:
    proj = sb_home / "projects" / slug
    proj.mkdir(parents=True)
    (proj / "project.env").write_text(
        f"SB_PROJECT_SLUG={slug}\nSB_GITHUB_REPO={repo}\nSB_BASE_BRANCH=main\n"
    )
    (proj / "WORKFLOW.md").write_text(template.read_text())
    return proj


PROTOTYPE_TEMPLATE = REPO_ROOT / "workflow" / "stances" / "WORKFLOW.prototype.md"
BASE_TEMPLATE = REPO_ROOT / "workflow" / "WORKFLOW.base.md"


@pytest.fixture()
def sb_home(tmp_path: Path) -> Path:
    """An SB_HOME with one prototype-shaped and one base-shaped project bound."""
    home = tmp_path / "sb"
    _project(home, "protoproj", "acme/proto", PROTOTYPE_TEMPLATE)
    _project(home, "baseproj", "acme/base", BASE_TEMPLATE)
    return home


def run_in(sb_home: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        input=stdin, capture_output=True, text=True, cwd=REPO_ROOT,
        env={**os.environ, "SB_HOME": str(sb_home)},
    )


def test_default_entry_on_base_project_is_triage(sb_home: Path) -> None:
    # Criterion 6: base behaviour is unchanged — no --entry still means triage,
    # and triage carries no gate:triage-passed stamp (a verifier grants that).
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "acme/base")
    assert proc.returncode == 0, proc.stderr
    assert "labels:     status:triage" in proc.stdout
    assert "gate:triage-passed" not in proc.stdout


def test_default_entry_on_prototype_project_is_dispatchable(sb_home: Path) -> None:
    # The bug: prototype dispatches todo/in progress/review and never triage, so
    # the default must land on a state the project actually moves.
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "acme/proto")
    assert proc.returncode == 0, proc.stderr
    assert "labels:     status:todo,gate:triage-passed" in proc.stdout
    assert "entry:      todo (resolved from active_states)" in proc.stdout


def test_dry_run_names_the_resolved_source_and_state_set(sb_home: Path) -> None:
    # Criterion 1's checkable form: the resolution is observable, not implied.
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "acme/proto")
    assert proc.returncode == 0, proc.stderr
    assert "project:    protoproj" in proc.stdout
    assert str(sb_home / "projects" / "protoproj" / "WORKFLOW.md") in proc.stdout
    assert "dispatches: todo, in progress, review" in proc.stdout


def test_unresolvable_project_refuses_rather_than_defaulting(sb_home: Path) -> None:
    # Criterion 2: never a silent fall-through to triage.
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "nobody/unbound")
    assert proc.returncode != 0
    assert "status:triage" not in proc.stdout
    assert "--entry" in proc.stderr
    assert "nobody/unbound" in proc.stderr


def test_ambiguous_binding_refuses(tmp_path: Path) -> None:
    # Two bindings for one repo: first-match would file against a state machine
    # the scheduler may not be the one using.
    home = tmp_path / "sb"
    _project(home, "one", "acme/dup", BASE_TEMPLATE)
    _project(home, "two", "acme/dup", PROTOTYPE_TEMPLATE)
    proc = run_in(home, "--dry-run", "--title", "T", "--repo", "acme/dup")
    assert proc.returncode != 0
    assert "ambiguous" in proc.stderr
    assert "--entry" in proc.stderr


def test_binding_without_composed_workflow_refuses(tmp_path: Path) -> None:
    home = tmp_path / "sb"
    proj = _project(home, "halfway", "acme/half", BASE_TEMPLATE)
    (proj / "WORKFLOW.md").unlink()
    proc = run_in(home, "--dry-run", "--title", "T", "--repo", "acme/half")
    assert proc.returncode != 0
    assert "WORKFLOW.md" in proc.stderr
    assert "--entry" in proc.stderr


def test_explicit_entry_survives_a_failed_resolution(sb_home: Path) -> None:
    # The refusal above names --entry as the fix, so --entry must still work on
    # exactly the path that refused. Otherwise the advice is a dead end.
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "nobody/unbound",
                  "--entry", "todo")
    assert proc.returncode == 0, proc.stderr
    assert "labels:     status:todo,gate:triage-passed" in proc.stdout
    assert "entry:      todo (explicit)" in proc.stdout


def test_explicit_undispatchable_entry_is_refused(sb_home: Path) -> None:
    # Criterion 5: the explicit case must not be silently worse than the default.
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "acme/proto",
                  "--entry", "triage")
    assert proc.returncode != 0
    assert "dispatches: todo, in progress, review" in proc.stderr
    assert "todo" in proc.stderr


def test_explicit_gate_entry_is_allowed_where_the_stance_gates_it(sb_home: Path) -> None:
    # `drafting` is not dispatched at base either — it is a declared GATE, where
    # a ticket legitimately waits for a human. Refusing it would break Gate A.
    proc = run_in(sb_home, "--dry-run", "--title", "T", "--repo", "acme/base",
                  "--entry", "drafting")
    assert proc.returncode == 0, proc.stderr
    assert "labels:     status:drafting" in proc.stdout


def test_bash_resolution_agrees_with_workflow_for_repo() -> None:
    # The invariant that keeps the bash mirror from becoming a second, drifting
    # repo->project map (AgDR-043): for a real binding, both must name the same
    # composed WORKFLOW.md.
    from orchestrator import status_board

    expected = status_board.workflow_for_repo("colin-prologue/Switchboard")
    assert expected is not None, "switchboard-self binding missing from projects/"
    proc = run("--dry-run", "--title", "T", "--repo", "colin-prologue/Switchboard")
    assert proc.returncode == 0, proc.stderr
    assert f"workflow:   {expected}" in proc.stdout


def test_dry_run_body_from_file(tmp_path: Path) -> None:
    body = tmp_path / "body.md"
    body.write_text("## Intent\n\nfrom a file\n")
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--entry", "todo",
               "--body-file", str(body))
    assert proc.returncode == 0, proc.stderr
    assert "from a file" in proc.stdout


def test_dry_run_omitted_optionals_render_as_none() -> None:
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--entry", "todo")
    assert proc.returncode == 0, proc.stderr
    assert "milestone:  (none)" in proc.stdout
    assert "blocked-by: (none)" in proc.stdout


def test_dry_run_makes_no_network_write(tmp_path: Path) -> None:
    # Prove no write happens, don't just read the banner: shadow `gh` with a
    # sentinel that records any invocation, and assert it was never called.
    fake_gh = tmp_path / "gh"
    marker = tmp_path / "gh-was-called"
    fake_gh.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--title", "T", "--repo", "o/n", "--entry", "todo"],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "no network writes" in proc.stdout.lower()
    assert not marker.exists(), "dry-run invoked gh"


def test_scaffold_output_is_valid_dry_run_body() -> None:
    # The skeleton --scaffold emits should feed straight back in as a body.
    scaffold = run("--scaffold")
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--entry", "todo",
               stdin=scaffold.stdout)
    assert proc.returncode == 0, proc.stderr
    for section in ("## In brief", "## Intent", "## Acceptance criteria", "## Non-goals", "## Assumptions"):
        assert section in proc.stdout


# --- real-filing path: MILESTONE_ARGS empty-array regression ------------------
#
# The reported bug (`MILESTONE_ARGS[@]: unbound variable`) lives in the
# real-filing path, AFTER the --dry-run early-exit — so --dry-run alone cannot
# cover it. We stub `gh` on PATH so the path runs network-free and assert it
# reaches `gh issue create` without aborting under `set -u`.
#
# Version note: bash < 4.4 (macOS system bash 3.2) is what makes "${arr[@]}" on
# an EMPTY array an unbound-variable error; bash >= 4.4 tolerates it. So on the
# dev box this is a hard regression guard; on newer bash it degrades to a smoke
# test of the same path. Either way it must exit 0 and invoke `gh issue create`.


def _gh_stub(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Install a fake `gh` on PATH; return (env, arglog). The stub records each
    invocation's argv and answers just enough for the real-filing path: an issue
    URL for `issue create`, a bare number for any `api` call (milestone lookup)."""
    arglog = tmp_path / "gh-args.log"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{arglog}"\n'
        'case "$1" in\n'
        '  issue) echo "https://github.com/owner/name/issues/123" ;;\n'
        "  api)   echo 7 ;;\n"
        "esac\n"
        "exit 0\n"
    )
    fake_gh.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return env, arglog


def test_real_filing_no_milestone_reaches_gh(tmp_path: Path) -> None:
    # Regression: no --milestone -> MILESTONE_ARGS is empty; the guarded
    # expansion must not trip `set -u`. Reproduces #... on bash 3.2.
    env, arglog = _gh_stub(tmp_path)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--title", "T", "--repo", "owner/name", "--entry", "todo"],
        input="body text\n", capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    assert proc.returncode == 0, f"real filing aborted: {proc.stderr}"
    assert "unbound variable" not in proc.stderr
    assert arglog.exists(), "gh was never invoked"
    calls = arglog.read_text()
    assert "issue create" in calls
    assert "created:" in proc.stdout


def test_real_filing_with_milestone_forwards_flag(tmp_path: Path) -> None:
    # Guard against an over-correction that drops the milestone: when set, the
    # array must still forward `--milestone <name>` to `gh issue create`.
    env, arglog = _gh_stub(tmp_path)
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--title", "T", "--repo", "owner/name", "--entry", "todo",
         "--milestone", "Sprint 3"],
        input="body text\n", capture_output=True, text=True, cwd=REPO_ROOT, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    create_call = next(
        (ln for ln in arglog.read_text().splitlines() if ln.startswith("issue create")),
        "",
    )
    assert "--milestone Sprint 3" in create_call, f"milestone not forwarded: {create_call!r}"


# --- validation --------------------------------------------------------------


def test_missing_title_fails() -> None:
    proc = run("--dry-run", "--repo", "o/n")
    assert proc.returncode != 0
    assert "title" in proc.stderr.lower()


@pytest.mark.parametrize("entry", ["drafting", "triage", "todo"])
def test_all_valid_entry_states_map(entry: str) -> None:
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--entry", entry)
    assert proc.returncode == 0, proc.stderr
    expected = f"status:{entry}" + (",gate:triage-passed" if entry == "todo" else "")
    assert f"labels:     {expected}" in proc.stdout


def test_invalid_entry_state_rejected() -> None:
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--entry", "in-progress")
    assert proc.returncode != 0
    assert "entry" in proc.stderr.lower()


def test_non_numeric_blocked_by_rejected() -> None:
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--entry", "todo",
               "--blocked-by", "12,abc")
    assert proc.returncode != 0
    assert "blocked-by" in proc.stderr.lower()


def test_bad_repo_shape_rejected() -> None:
    proc = run("--dry-run", "--title", "T", "--repo", "not-a-slug")
    assert proc.returncode != 0
    assert "repo" in proc.stderr.lower()


def test_unknown_flag_rejected() -> None:
    proc = run("--dry-run", "--title", "T", "--repo", "o/n", "--bogus")
    assert proc.returncode != 0
