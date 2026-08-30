"""Tests for the shipped-but-unwired audit (issue #172).

Two halves, and the second is the point of the ticket:

  * the pure function — what counts as "at its disabling default", what a
    per-project exemption silences, and that the policy is table-driven rather
    than a scan for empty-looking values;
  * the WIRING — `scripts/freshness-preflight.sh` actually invoking it against
    the composed bytes it just produced. A pytest-only check would itself be a
    shipped feature that never runs against real bytes, which is the failure
    this ticket exists to stop.

Every fixture uses a made-up slug: `switchboard-self` is the only project in
this repository, and no criterion here asserts anything about it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from orchestrator.disabling_defaults import (
    COMPLEX,
    MISSING,
    REASON_DEFAULT,
    REASON_UNSET,
    AuditError,
    TableError,
    audit_composed_workflow,
    field_value,
    front_matter,
    load_table,
    parse_table,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFLIGHT = REPO_ROOT / "scripts" / "freshness-preflight.sh"
PACKAGE = REPO_ROOT / "orchestrator" / "src" / "orchestrator"
SHIPPED_TABLE = REPO_ROOT / "workflow" / "disabling-defaults.yml"

TABLE = """\
defaults:
  fold.operator_logins: []
  review_response.bot_logins: []

deliberately_off: {}
"""

# A composed workflow shaped like the real one: the two audited fields sit
# under nested maps, and `hooks:` carries block scalars that no subset YAML
# reader should try to hold. The audit must survive them by reading only the
# lines on the path it was asked about.
COMPOSED = """\
---
tracker:
  kind: github
  repo: "acme/widgets"
  active_states: ["triage", "todo"]

hooks:
  after_create: |
    "$SB_HOME/hooks/after_create.sh"
  before_run: |
    "$SB_HOME/hooks/before_run.sh"
  timeout_ms: 120000

fold:
  operator_logins: [{fold}]

# OFF BY DEFAULT. SHIPPED EMPTY ON PURPOSE — this sentence documents the
# TEMPLATE's default, not any project's decision.
review_response:
  bot_logins: [{bots}]
---
prompt body
"""


def composed(tmp_path: Path, *, fold: str = "", bots: str = "",
             name: str = "composed-WORKFLOW.md") -> Path:
    path = tmp_path / name
    path.write_text(COMPOSED.format(fold=fold, bots=bots))
    return path


def table(tmp_path: Path, text: str = TABLE) -> Path:
    path = tmp_path / "disabling-defaults.yml"
    path.write_text(text)
    return path


def fields(findings) -> list[str]:
    return [f.field for f in findings]


# --- the check ---------------------------------------------------------------

def test_reports_both_instances_and_neither_when_they_are_populated(tmp_path):
    """AC2 — one test, both directions, so it cannot pass by reporting nothing."""
    empty = audit_composed_workflow(
        "demo", composed(tmp_path, name="empty.md"), table(tmp_path))
    assert fields(empty) == ["fold.operator_logins", "review_response.bot_logins"]
    assert {f.reason for f in empty} == {REASON_DEFAULT}

    populated = audit_composed_workflow(
        "demo",
        composed(tmp_path, fold='"ada"', bots='"review-bot"', name="full.md"),
        table(tmp_path),
    )
    assert populated == []


def test_a_deliberately_off_field_is_silenced_and_only_that_field(tmp_path):
    """AC3 — the exemption is per project AND per field, not a global mute."""
    findings = audit_composed_workflow(
        "demo",
        composed(tmp_path),
        table(tmp_path, TABLE.replace(
            "deliberately_off: {}",
            "deliberately_off:\n  demo:\n    - review_response.bot_logins\n")),
    )
    assert fields(findings) == ["fold.operator_logins"]


def test_template_prose_is_not_an_exemption(tmp_path):
    """AC4 — the composed bytes carry "SHIPPED EMPTY ON PURPOSE" and the
    project has no `deliberately_off` entry, so the field is still reported.
    That comment documents the value every project starts from; reading it as
    a decision would mute the check on the one instance that proved it works."""
    text = composed(tmp_path).read_text()
    assert "SHIPPED EMPTY ON PURPOSE" in text

    findings = audit_composed_workflow("demo", composed(tmp_path), table(tmp_path))
    assert "review_response.bot_logins" in fields(findings)


def test_an_exemption_belongs_to_one_slug_only(tmp_path):
    """A neighbouring project's decision is not this project's."""
    policy = table(tmp_path, TABLE.replace(
        "deliberately_off: {}",
        "deliberately_off:\n  other:\n    - review_response.bot_logins\n"))
    findings = audit_composed_workflow("demo", composed(tmp_path), policy)
    assert "review_response.bot_logins" in fields(findings)


def test_audits_the_composed_config_not_the_tracked_template(tmp_path):
    """AC5 — a project whose TRACKED workflow has both fields populated and
    whose COMPOSED file has both empty is unwired, and must be reported as
    such: the composed file is the one the orchestrator loads."""
    project = tmp_path / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "WORKFLOW.md").write_text(
        COMPOSED.format(fold='"ada"', bots='"review-bot"'))
    run_dir = tmp_path / ".run" / "demo"
    run_dir.mkdir(parents=True)
    live = run_dir / "composed-WORKFLOW.md"
    live.write_text(COMPOSED.format(fold="", bots=""))

    findings = audit_composed_workflow("demo", live, table(tmp_path))
    assert fields(findings) == [
        "fold.operator_logins", "review_response.bot_logins"]


def test_the_policy_is_table_driven_not_a_scan_for_empty_values(tmp_path):
    """AC6 — a field whose disabling value is NON-empty is reported when it
    holds that value and not when it holds another. A scan for empty-looking
    values could not tell these two files apart."""
    policy = table(tmp_path, 'defaults:\n  example.mode: "never"\n')
    off = tmp_path / "off.md"
    off.write_text('---\nexample:\n  mode: "never"\n---\nbody\n')
    on = tmp_path / "on.md"
    on.write_text('---\nexample:\n  mode: "always"\n---\nbody\n')

    assert fields(audit_composed_workflow("demo", off, policy)) == ["example.mode"]
    assert audit_composed_workflow("demo", on, policy) == []


def test_an_empty_field_outside_the_table_is_not_reported(tmp_path):
    """The other half of table-driven: this is not a general config linter."""
    policy = table(tmp_path, "defaults:\n  fold.operator_logins: []\n")
    findings = audit_composed_workflow("demo", composed(tmp_path), policy)
    assert fields(findings) == ["fold.operator_logins"]


def test_a_declared_field_absent_from_the_composed_config_is_reported(tmp_path):
    """Deleting the block is the same silence as emptying the list, so it earns
    the same finding — otherwise the audit goes quiet exactly when a feature
    disappears from the config altogether."""
    path = tmp_path / "bare.md"
    path.write_text("---\ntracker:\n  kind: github\n---\nbody\n")
    findings = audit_composed_workflow("demo", path, table(tmp_path))
    assert fields(findings) == [
        "fold.operator_logins", "review_response.bot_logins"]
    assert {f.reason for f in findings} == {REASON_UNSET}


def test_a_block_sequence_counts_as_populated(tmp_path):
    """A hand-edited composed file may write the list in block form; reading
    that as empty would be a false positive on a feature that IS enabled."""
    path = tmp_path / "block.md"
    path.write_text(
        "---\nfold:\n  operator_logins:\n    - ada\n"
        "review_response:\n  bot_logins: []\n---\nbody\n")
    findings = audit_composed_workflow("demo", path, table(tmp_path))
    assert fields(findings) == ["review_response.bot_logins"]


def test_a_missing_composed_workflow_raises_rather_than_reporting_clean(tmp_path):
    with pytest.raises(AuditError):
        audit_composed_workflow("demo", tmp_path / "nope.md", table(tmp_path))


# --- reading ------------------------------------------------------------------

def test_field_value_normalizes_both_sides_of_the_comparison(tmp_path):
    front = front_matter(COMPOSED.format(fold="", bots='"review-bot"'))
    assert field_value(front, "fold.operator_logins") == ()
    assert field_value(front, "review_response.bot_logins") == ("review-bot",)
    assert field_value(front, "tracker.repo") == "acme/widgets"
    assert field_value(front, "fold.nope") is MISSING
    assert field_value(front, "nope.nope") is MISSING
    # `hooks` is a map, not a comparable value — never equal to a disabling
    # default, and never a reason to fail the whole audit.
    assert field_value(front, "hooks") is COMPLEX
    assert field_value(front, "hooks.timeout_ms") == 120000


def test_front_matter_stops_at_the_closing_delimiter():
    front = front_matter(COMPOSED.format(fold="", bots=""))
    assert "prompt body" not in front
    assert "fold:" in front


def test_the_shipped_table_parses_and_declares_both_known_instances():
    policy = load_table(SHIPPED_TABLE)
    assert policy.defaults == {
        "fold.operator_logins": (),
        "review_response.bot_logins": (),
    }
    assert policy.deliberately_off == {}


def test_a_malformed_table_raises_rather_than_parsing_to_an_empty_policy():
    """An empty policy reports nothing and looks exactly like a clean project,
    so the reader refuses what it does not understand."""
    with pytest.raises(TableError):
        parse_table("defaults:\n  fold.operator_logins\n")
    with pytest.raises(TableError):
        parse_table("surprise:\n  x: 1\n")
    with pytest.raises(TableError):
        load_table(Path("/nonexistent/disabling-defaults.yml"))


# --- the wiring ---------------------------------------------------------------

BASE_TEMPLATE = """\
---
repo: {{REPO}}
workspace:
  root: {{WORKSPACE_ROOT}}
pool:
  max_concurrent_agents: {{MAX_AGENTS}}
convention_root: {{CONVENTION_ROOT}}
fold:
  operator_logins: [{{OPERATOR_LOGIN_YAML}}]
review_response:
  bot_logins: [{{REVIEW_BOT_YAML}}]
---
prompt body
"""

# The TRACKED snapshot deliberately disagrees with what origin composes: both
# fields are populated here and empty after composition. A check reading the
# tracked file would report clean, so a finding at all proves the composed
# path is the one that was audited.
TRACKED_WORKFLOW = """\
---
repo: acme/widgets
pool:
  max_concurrent_agents: 3
fold:
  operator_logins: ["ada"]
review_response:
  bot_logins: ["review-bot"]
---
tracked snapshot
"""


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t.t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True)
    return proc.stdout.strip()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A committed skeleton plus a local bare origin — the preflight recomposes
    from origin, so the template it reads has to be pushed there."""
    home = tmp_path / "sb"
    (home / "scripts").mkdir(parents=True)
    shutil.copy(PREFLIGHT, home / "scripts" / "freshness-preflight.sh")
    (home / "workflow").mkdir()
    (home / "workflow" / "WORKFLOW.base.md").write_text(BASE_TEMPLATE)
    shutil.copy(SHIPPED_TABLE, home / "workflow" / "disabling-defaults.yml")
    # The preflight runs the audit with a bare `python3` and PYTHONPATH pointed
    # here, so the module has to be importable from the checkout under test.
    pkg = home / "orchestrator" / "src" / "orchestrator"
    pkg.mkdir(parents=True)
    shutil.copy(PACKAGE / "__init__.py", pkg / "__init__.py")
    shutil.copy(PACKAGE / "disabling_defaults.py", pkg / "disabling_defaults.py")
    project = home / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "project.env").write_text(
        "SB_PROJECT_SLUG=demo\nSB_GITHUB_REPO=acme/widgets\nSB_BASE_BRANCH=main\n"
        "SB_WORKSPACE_ROOT=/tmp/ws\nSB_CONVENTION_ROOT=\n")
    (project / "WORKFLOW.md").write_text(TRACKED_WORKFLOW)
    (home / ".gitignore").write_text(".run/\n")

    git(home, "init", "-b", "main", "-q")
    git(home, "add", "-A")
    git(home, "commit", "-qm", "skeleton")
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", "-q", str(bare))
    git(home, "remote", "add", "origin", str(bare))
    git(home, "push", "-q", "origin", "main")
    return home


def run_preflight(home: Path, slug: str = "demo", **env_extra: str):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SB_")}
    env["SB_HOME"] = str(home)
    env["SB_LAUNCH_SHA"] = git(home, "rev-parse", "HEAD")
    env.update(env_extra)
    return subprocess.run(
        ["bash", str(home / "scripts" / "freshness-preflight.sh"), slug],
        env=env, cwd=str(home), capture_output=True, text=True)


def test_preflight_audits_the_file_it_just_recomposed(home):
    """AC7 — the check runs from the launch path, against the composed bytes,
    and its findings reach the existing stderr warning channel.

    The tracked WORKFLOW.md in this fixture has both fields POPULATED; only the
    recomposed file has them empty. So these two warnings can only come from
    the composed path having been the audited one."""
    proc = run_preflight(home)
    assert proc.returncode == 0, proc.stderr

    live = home / ".run" / "demo" / "composed-WORKFLOW.md"
    assert live.exists()
    assert "operator_logins: []" in live.read_text()
    assert 'operator_logins: ["ada"]' in (
        home / "projects" / "demo" / "WORKFLOW.md").read_text()

    assert "[freshness] unwired feature in 'demo': fold.operator_logins" in proc.stderr
    assert "review_response.bot_logins" in proc.stderr


def test_preflight_is_silent_once_the_features_are_wired(home):
    """The other direction through the shell: a project whose binding names an
    operator and a review bot composes populated lists and earns no warning."""
    env_file = home / "projects" / "demo" / "project.env"
    env_file.write_text(env_file.read_text()
                        + 'SB_OPERATOR_LOGIN="ada"\nSB_REVIEW_BOT="review-bot"\n')
    proc = run_preflight(home)
    assert proc.returncode == 0, proc.stderr
    assert "unwired" not in proc.stderr


def test_a_malformed_table_warns_and_still_exits_zero(home):
    """AC8 — the audit is fail-open. This script runs under run-project.sh's
    `set -euo pipefail`, where a non-zero fail-open path is a hard launch
    refusal — i.e. not fail-open at all."""
    (home / "workflow" / "disabling-defaults.yml").write_text(
        "defaults:\n      this is not a table\n")
    proc = run_preflight(home)
    assert proc.returncode == 0, proc.stderr
    assert "unwired audit skipped for 'demo'" in proc.stderr


def test_a_missing_table_warns_and_still_exits_zero(home):
    (home / "workflow" / "disabling-defaults.yml").unlink()
    proc = run_preflight(home)
    assert proc.returncode == 0, proc.stderr
    assert "unwired audit skipped for 'demo'" in proc.stderr


def test_an_unusable_interpreter_warns_and_still_exits_zero(home):
    """The audit's own launch failure is a warning too: it must not be able to
    turn a missing interpreter into a refused launch."""
    proc = run_preflight(home, SB_PYTHON=str(home / "no-such-python"))
    assert proc.returncode == 0, proc.stderr
    assert "[freshness]" in proc.stderr
