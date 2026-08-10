"""Worker merge guard — Gate C by mechanism (issue #133, AgDR-036).

The guard denies an ENUMERATED set of Gate-C-violating Bash shapes. It is not a
security boundary (denials are soft; `gh api …/pulls/{n}/merge` is a recorded
residual) — it raises the cost of a violation and makes every attempt
observable. These tests pin the enumeration in both directions: the denied
shapes deny, and the free-text/read-only shapes that share their tokens allow.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from orchestrator.runner import GUARD_MATCHER, GUARD_PATH, _write_guard_settings

DENIAL_PREFIX = "switchboard-guard: denied:"


def _run_guard_env(payload: dict, env: dict) -> subprocess.CompletedProcess:
    """`_run_guard` (test_audit_fixes.py:77) hard-codes CLAUDE_PROJECT_DIR and
    so cannot express the no-workspace case; this variant takes the env."""
    return subprocess.run(
        [sys.executable, "-I", str(GUARD_PATH)],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
    )


def _run_bash(command: str, workspace: Path) -> subprocess.CompletedProcess:
    return _run_guard_env(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        {"CLAUDE_PROJECT_DIR": str(workspace), "PATH": "/usr/bin:/bin"},
    )


DENIED = [
    # gh pr merge — any flag order, with/without -R o/r
    "gh pr merge 12",
    "gh pr merge 12 --squash --delete-branch",
    "gh pr merge --squash -R colin-prologue/Switchboard 12",
    "gh pr merge",
    # gh pr review --approve — flag before or after the PR number
    "gh pr review 12 --approve",
    "gh pr review --approve 12",
    'gh pr review 12 --approve --body "lgtm"',
    # gh pr close
    "gh pr close 12",
    "gh pr close 12 --comment 'abandoned'",
    # force pushes
    "git push --force",
    "git push --force origin switchboard/issue-133",
    "git push --force-with-lease",
    "git push --force-with-lease origin switchboard/issue-133",
    "git push -f",
    "git push -f origin switchboard/issue-133",
    # the +refspec force form
    "git push origin +switchboard/issue-133",
    "git push origin +refs/heads/main:refs/heads/main",
]

ALLOWED = [
    # read-only / non-merging gh pr verbs
    "gh pr view 12",
    "gh pr comment 12 --body 'ready for review'",
    "gh pr diff 12",
    "gh pr create --title 'merge guard' --body 'implements #133'",
    "gh pr review 12 --comment --body 'a note'",
    # non-force pushes
    "git push",
    "git push origin switchboard/issue-133",
    "git push -u origin switchboard/issue-133",
    # gh api is a recorded residual, not a matched shape
    "gh api repos/o/r/pulls/12",
    # free text: the verbs appear inside quoted arguments, never in verb
    # position. Denying these would strand the MANDATORY handoff step
    # (WORKFLOW.base.md:339-341) — this ticket's own PR first.
    'gh pr create --title "merge guard" --body "denies gh pr merge; a human will merge"',
    'gh pr comment 12 --body "ready to merge"',
    'gh issue comment 133 --body "… close …"',
    # anchor-binding: both fail under any first-`pr`-anywhere reading
    'gh issue comment 133 --body "the guard denies gh pr merge here"',
    'gh pr comment 12 --body "gh pr merge is Colin\'s call"',
]


@pytest.mark.parametrize("command", DENIED)
def test_guard_denies_gate_c_shapes(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2, r
    assert r.stderr.startswith(DENIAL_PREFIX), r.stderr
    assert "hand off, don't self-merge" in r.stderr


@pytest.mark.parametrize("command", ALLOWED)
def test_guard_allows_everything_else(command, tmp_path):
    r = _run_bash(command, tmp_path)
    # A PreToolUse hook's "allow" is exit 0 with empty stdout — there is no
    # stdin passthrough.
    assert r.returncode == 0, r
    assert r.stdout == ""


def test_merge_deny_is_workspace_independent():
    """No CLAUDE_PROJECT_DIR and no `cwd` in the payload: the merge deny must
    not ride behind guard.py's no-workspace early return."""
    r = _run_guard_env(
        {"tool_name": "Bash", "tool_input": {"command": "gh pr merge 12"}},
        {"PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 2, r
    assert r.stderr.startswith(DENIAL_PREFIX), r.stderr


def test_unbalanced_quote_falls_back_to_naive_split(tmp_path):
    """shlex raises on an unbalanced quote; the fallback must still evaluate,
    never silently allow."""
    r = _run_bash("gh pr merge 12 --body 'oops", tmp_path)
    assert r.returncode == 2, r


def test_bashoutput_is_a_harmless_matcher_superset(tmp_path):
    """The settings matcher alternation is unanchored, so `Bash` also matches
    `BashOutput` — no `command` key => exit 0."""
    r = _run_guard_env(
        {"tool_name": "BashOutput", "tool_input": {"bash_id": "1"}},
        {"CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0
    assert r.stdout == ""


def test_guard_settings_matcher_includes_bash(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    matcher = json.loads(_write_guard_settings(ws).read_text())[
        "hooks"]["PreToolUse"][0]["matcher"]
    assert matcher == GUARD_MATCHER
    assert "Bash" in matcher.split("|")
    # additive: the containment matchers survive
    for old in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        assert old in matcher.split("|")


# --- bundled short flags (codex review, PR #136) ------------------------------

@pytest.mark.parametrize("command", [
    "gh pr review 12 -a",
    "gh pr review 12 -am lgtm",
    "git push -fu origin branch",
    "git push -uf origin branch",
])
def test_short_and_bundled_flags_are_denied(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2
    assert "switchboard-guard: denied:" in r.stderr


@pytest.mark.parametrize("command", [
    "git push -u origin branch",          # set-upstream alone is fine
    "gh pr review 12 --comment ok",       # review without approve
])
def test_adjacent_short_flags_stay_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0
    assert r.stdout == ""


# --- flag values and global options (codex review round 2, PR #136) -----------

@pytest.mark.parametrize("command", [
    "gh -R colin-prologue/Switchboard pr merge 12",
    "gh --repo colin-prologue/Switchboard pr merge 12",
    "gh pr -R colin-prologue/Switchboard merge 12",
    "gh pr review 12 --approve=true",
    "git -C . push -f origin branch",
    "git -c user.name=x push --force origin branch",
    "git --attr-source HEAD push -f origin main",          # r10: separated value
    "git --attr-source=HEAD push -f origin main",
])
def test_flag_values_and_globals_do_not_bypass(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "gh pr review 12 -c -b=thanks",       # attached body value carrying 'a'? no — but '=' stops the scan
    "gh pr review 12 -ba",                # -b takes a value; 'a' is the VALUE
    "gh pr review 12 -b amazing",
    "git push -ofoo origin branch",       # -o's attached value carries 'f'
    "git -C . push origin branch",        # global option, no force
])
def test_attached_values_are_not_flags(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0
    assert r.stdout == ""


# --- config-env and separated push-option values (round 3, PR #136) -----------

def test_config_env_global_does_not_bypass(tmp_path):
    r = _run_bash("git --config-env user.name=FOO push -f origin branch", tmp_path)
    assert r.returncode == 2


def test_separated_push_option_value_is_not_a_refspec(tmp_path):
    r = _run_bash("git push -o +foo origin branch", tmp_path)
    assert r.returncode == 0
    # ...but a real +refspec after the consumed option still denies
    r2 = _run_bash("git push -o opt origin +main", tmp_path)
    assert r2.returncode == 2


# --- abbreviations, mirror, separated body values (round 4, PR #136) ----------

@pytest.mark.parametrize("command", [
    "git push --force-w origin branch",
    "git push --force-with-l origin branch",
    "git push --force-if-includes --force-with-lease origin branch",
    "git push --mirror origin",
    "git push --mir origin",
    "git push --mi origin",
    "git push --m origin",
])
def test_abbreviations_and_mirror_are_denied(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


def test_separated_body_value_is_not_an_approval(tmp_path):
    r = _run_bash("gh pr review 12 --comment --body --approve", tmp_path)
    assert r.returncode == 0
    # ...but an --approve OUTSIDE a body value still denies
    r2 = _run_bash("gh pr review 12 --body ok --approve", tmp_path)
    assert r2.returncode == 2


def test_repository_operand_starting_with_plus_is_not_a_refspec(tmp_path):
    r = _run_bash("git push --dry-run +remote HEAD", tmp_path)
    assert r.returncode == 0
    # ...a refspec AFTER the repository still denies
    r2 = _run_bash("git push +remote +main", tmp_path)
    assert r2.returncode == 2
    # a separated push-option value must not count as the repository
    # operand (round 11: --receive-pack's value shifted +remote into
    # refspec position)
    r3 = _run_bash(
        "git push --dry-run --receive-pack git-receive-pack +remote HEAD",
        tmp_path)
    assert r3.returncode == 0
    r4 = _run_bash(
        "git push --receive-pack git-receive-pack origin +main", tmp_path)
    assert r4.returncode == 2
    # round 13: git-style abbreviations of value-taking options consume too
    r5 = _run_bash(
        "git push --dry-run --recei git-receive-pack +remote HEAD", tmp_path)
    assert r5.returncode == 0
    r6 = _run_bash("git push --recei git-receive-pack origin +main", tmp_path)
    assert r6.returncode == 2
    # an in-list-ambiguous prefix is not consumed (git rejects it anyway),
    # so a force refspec after it still denies
    r7 = _run_bash("git push --rec x origin +main", tmp_path)
    assert r7.returncode == 2


# --- config-injected force + attached repo selector (round 6, PR #136) --------

@pytest.mark.parametrize("command", [
    "git -c remote.origin.push=+HEAD:refs/heads/main push origin",
    "git -cremote.origin.push=+HEAD:refs/heads/main push origin",
    "git --config-env remote.origin.push=FORCE_VAR push origin",
])
def test_config_injected_force_is_denied(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "git -c user.name=x push origin branch",             # non-push config fine
    "git -c remote.origin.push=HEAD push origin",        # non-force push config fine
    "gh pr review 1 -Ro/a --comment -b ok",              # attached repo value with 'a'
])
def test_benign_config_and_attached_repo_are_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


# --- case-folded config keys + mirror-by-config (round 7, PR #136) ------------

@pytest.mark.parametrize("command", [
    "git -c remote.origin.Push=+HEAD:refs/heads/main push origin",
    "git -c Remote.origin.PUSH=+HEAD:refs/heads/main push origin",
    "git -cremote.origin.Push=+HEAD:refs/heads/main push origin",
    "git --config-env remote.origin.PUSH=FORCE_VAR push origin",
    "git -c remote.origin.mirror=true push origin",
    "git -c Remote.origin.Mirror=true push origin",
    "git -cremote.origin.mirror=true push origin",
    "git --config-env remote.origin.mirror=MIRROR_VAR push origin",
])
def test_casefolded_and_mirror_config_are_denied(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "git -c remote.origin.pushurl=https://x push origin",  # .pushurl is not .push
    "git -c mirror.something=true push origin",            # not remote.<name>.mirror
])
def test_near_miss_config_keys_are_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


# --- help discovery (round 14, PR #136) ---------------------------------------
# `gh pr merge --help` stays DENIED by design: cobra consumes `--help` as the
# value of a preceding value-flag (`--body --help` merges, no help shown), so
# token-presence help detection is a bypass class; the guard's smoke canary
# (#133 human gate) also pins the denial. Discovery goes through `gh help`.

@pytest.mark.parametrize("command", [
    "gh help pr merge",
    "gh help pr close",
    "gh pr --help",
])
def test_gh_help_forms_are_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


def test_help_flag_on_denied_verb_stays_denied(tmp_path):
    r = _run_bash("gh pr merge --help", tmp_path)
    assert r.returncode == 2


# --- bundles ending in a value-taker consume the next token (round 15) --------

@pytest.mark.parametrize("command", [
    "gh pr review 12 -cb --approve",                   # --approve is the body
    "git push --dry-run -vo opt +remote HEAD",         # opt is the o value
])
def test_bundle_final_value_taker_consumes_next(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


@pytest.mark.parametrize("command", [
    "gh pr review 12 -cb thanks -a",                   # real approve after value
    "gh pr review 12 -ab whatever",                    # a before the value-taker
    "git push -vo opt origin +main",                   # real force refspec after
    "git push -vf origin main",                        # f in bundle still denies
])
def test_bundle_denials_survive_value_consumption(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


# --- line continuations, non-push config, alias smuggling (round 9, PR #136) --

@pytest.mark.parametrize("command", [
    "gh \\\npr merge 12",
    "git push \\\n-f origin branch",
    "git \\\npush --mirror origin",
])
def test_backslash_newline_continuations_are_denied(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "git -c remote.origin.push=+main status",              # read-only command
    "git -c remote.origin.mirror=true config --list",      # read-only command
    "git --config-env remote.origin.push=VAR log",         # read-only command
])
def test_push_config_on_non_push_commands_is_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


@pytest.mark.parametrize("command", [
    "git -c alias.x='push -f' x origin",                   # alias renames the verb
    "git -c Alias.deploy=push deploy -f origin",
    "git -calias.x=push x",
    "git --config-env alias.x=CMD_VAR x",
    "git -C . -c alias.x='push -f' x origin",              # r12: global value
    "git --git-dir .git -c remote.origin.mirror=true push origin",
    "git -C . --attr-source HEAD -c remote.origin.push=+x push origin",
])
def test_alias_config_is_denied(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2
