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

from orchestrator import guard
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


# --- bash quoting forms shlex does not know (round 16, PR #136) ---------------

@pytest.mark.parametrize("command", [
    "gh $'pr' merge 12",
    "git $'push' -f origin main",
    "gh pr review 12 $'--approve'",
    "gh $'\\x70r' merge 12",                           # \x70 = 'p'
    "git $'pu'$'sh' --mirror origin",                  # concatenation
    'gh $"pr" merge 12',                               # locale quoting
])
def test_ansi_c_and_locale_quoting_are_normalized(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


def test_ansi_c_in_benign_value_is_allowed(tmp_path):
    r = _run_bash("git commit -m $'multi\\nline msg'", tmp_path)
    assert r.returncode == 0


# --- literal $' inside ordinary quotes is text, not syntax (round 17) ---------

@pytest.mark.parametrize("command", [
    "gh pr create --body \"mention $' syntax\"",
    "gh pr create --body 'literal $\"x\" here'",
    "gh pr comment 5 --body \"the $'form' is neat\"",
])
def test_quoted_ansi_c_markers_are_literal(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


def test_unterminated_bare_ansi_c_denies(tmp_path):
    r = _run_bash("gh $'pr merge 12", tmp_path)
    assert r.returncode == 2


# --- heredoc bodies are data; redirections cannot hide verbs (round 18) -------

@pytest.mark.parametrize("command", [
    "gh pr create --body-file - <<'EOF'\nDocument the literal $' form\nEOF",
    "gh pr create -t x --body-file - <<EOF\nbody with $'x' and \" quote\nEOF",
])
def test_heredoc_bodies_are_data(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


@pytest.mark.parametrize("command", [
    "git <<X push -f origin main\nbody\nX",                # attached delimiter
    "git << X push -f origin main\nbody\nX",               # separated delimiter
    "git <<-'DELIM' push --mirror origin\nbody\nDELIM",    # <<- + quoted word
    "gh <<< data pr merge 12",                             # herestring
    "git push -f origin main <<'EOF'\n$'\nEOF",            # verb before heredoc
    "gh </dev/null pr merge 12",                           # r19: ordinary redirs
    "git 2>/dev/null push -f origin main",
    "gh pr >log merge 12",
    "git push -f >out 2>&1 origin main",
    "gh {fd}>/dev/null pr merge 12",                       # r21: brace fd alloc
    "git {fd}>/dev/null push -f origin main",
])
def test_redirections_cannot_hide_denied_verbs(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "git push origin main >push.log 2>&1",                 # trailing redirs fine
    "gh pr view 12 >file",
    "git commit -m 'msg with > arrow'",                    # quoted > is text
    'gh pr comment 5 --body "2>&1 is a redirection"',
    "git push origin a2b",                                 # digit inside a word
    "git push origin main {log}>out.txt",                  # trailing brace fd
    "git commit -m '{fd}> is bash syntax'",                # quoted brace fd
])
def test_benign_redirections_and_quoted_arrows_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


# --- process substitutions are arguments, not redirections (round 20) ---------

@pytest.mark.parametrize("command", [
    "gh pr review 1 --body <(echo) --approve",             # r20 repro: value shift
    "git push -o <(echo) origin +HEAD:main",               # r20 repro: positional
    "git < <(echo x y) push -f origin main",               # procsub redir target
])
def test_process_substitutions_do_not_shift_denials(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "gh pr review 1 --body <(echo gen) -c",                # procsub as body value
    "git push origin main < <(echo y)",                    # trailing procsub redir
])
def test_benign_process_substitutions_allowed(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


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


# --- brace expansion + comments (round 22, PR #136) ---------------------------

@pytest.mark.parametrize("command", [
    "gh pr m{e..e}rge 12",
    "git push --for{c..c}e origin main",
    "gh pr m{e,x}rge 12",                      # expands: merge mxrge -> verb merge
    "g{i..i}t push -f origin main",
    "gh pr m{e..e..1}rge 12",                  # r23: stepped char range
    "git push --for{c..c..1}e origin main",
])
def test_brace_expansion_cannot_spell_denied_verbs(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 2


@pytest.mark.parametrize("command", [
    "git push origin branch # never --force",
    "gh pr review 12 -c -b ok # never --approve",
    "gh pr comment 5 --body 'has # hash and {a,b} braces'",
    "git commit -m 'msg with {1..3}'",
    "git push origin 'br{a}nch'",              # quoted braces stay literal
    "mkdir -p a/{b,c}/d",                      # non-git/gh expansion unaffected
    "git add " + " ".join(f"f{i}.txt" for i in range(300)),  # r23: cap is growth-only
])
def test_comments_and_quoted_braces_are_not_flags(command, tmp_path):
    r = _run_bash(command, tmp_path)
    assert r.returncode == 0


# --- Gate C ownership is a per-project stance property -----------------------
#
# The guard shipped denying `gh pr merge` unconditionally, which silently
# revoked the merge right from every project whose stance dispatches an agent
# to the review state — civ-life's prototype QA had merged two PRs on its own
# the same day. These tests pin the relaxation in BOTH directions and, more
# importantly, pin how NARROW it is.

OWN_REPO = "colin-prologue/civ-life"


def _run_bash_gate_c_agent(command: str, workspace: Path,
                           repo: str = OWN_REPO) -> subprocess.CompletedProcess:
    """The guard as runner.py invokes it for an agent-owned Gate C."""
    return subprocess.run(
        [sys.executable, "-I", str(GUARD_PATH),
         f"{guard.GATE_C_AGENT_FLAG}{repo}"],
        input=json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True,
        env={"CLAUDE_PROJECT_DIR": str(workspace), "PATH": "/usr/bin:/bin"},
    )


@pytest.mark.parametrize("command", [
    "gh pr merge 12",
    "gh pr merge 12 --squash --delete-branch",
    "gh pr merge --squash -R colin-prologue/civ-life 12",   # own repo, explicit
    "gh pr merge --repo=colin-prologue/civ-life 12",         # attached form
    "gh pr merge -Rcolin-prologue/civ-life 12",              # short attached
    "gh pr merge https://github.com/colin-prologue/civ-life/pull/12",
    "gh $'pr' merge 12",          # the obfuscated forms relax too, or the
    "gh pr m{e..e}rge 12",        # relaxation would be trivially inconsistent
])
def test_merge_is_allowed_when_an_agent_owns_gate_c(command, tmp_path):
    proc = _run_bash_gate_c_agent(command, tmp_path)
    assert proc.returncode == 0, f"{command!r} denied: {proc.stderr}"


@pytest.mark.parametrize("command", [
    "gh pr merge 12",
    "gh pr merge --squash -R colin-prologue/Switchboard 12",
])
def test_merge_is_still_denied_when_the_flag_is_absent(command, tmp_path):
    """Omission denies. An older settings file, a hand-run hook, or a caller
    that forgets to thread the flag must not widen the guard."""
    proc = _run_bash(command, tmp_path)
    assert proc.returncode == 2
    assert DENIAL_PREFIX in proc.stderr


@pytest.mark.parametrize("command", [
    # approval is the REVIEWER's act; self-approval defeats it whoever holds
    # the gate
    "gh pr review 12 --approve",
    "gh pr review --approve 12",
    # closing is abandonment, not review
    "gh pr close 12",
    # history destruction is not a Gate C question at all
    "git push --force",
    "git push -f origin switchboard/issue-10",
    "git push --force-with-lease",
])
def test_only_merge_relaxes__approve_close_and_force_push_never_do(command, tmp_path):
    """The switch is Gate C, not trust-the-agent. If this test ever goes green
    for a new shape, the flag has quietly become a general permission."""
    proc = _run_bash_gate_c_agent(command, tmp_path)
    assert proc.returncode == 2, f"{command!r} was allowed by the Gate C flag"
    assert DENIAL_PREFIX in proc.stderr


def test_settings_file_carries_the_flag_only_when_told(tmp_path):
    """The wiring, not just the parser: whatever runner.py writes into the
    settings file is what Claude Code actually invokes."""
    ws = tmp_path / "ws"
    ws.mkdir()

    human = json.loads(_write_guard_settings(ws).read_text())
    human_cmd = human["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert guard.GATE_C_AGENT_FLAG not in human_cmd

    agent = json.loads(_write_guard_settings(ws, OWN_REPO).read_text())
    agent_cmd = agent["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert f"{guard.GATE_C_AGENT_FLAG}{OWN_REPO}" in agent_cmd
    assert str(GUARD_PATH) in agent_cmd


def test_default_write_is_the_human_gate(tmp_path):
    """`_write_guard_settings(ws)` with no second argument must not widen."""
    ws = tmp_path / "ws"
    ws.mkdir()
    cmd = json.loads(_write_guard_settings(ws).read_text(
        ))["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "--gate-c-owner" not in cmd


# --- the predicate itself ----------------------------------------------------

def test_agent_owns_gate_c_is_derived_from_dispatch_not_from_a_flag():
    from orchestrator.types import TrackerConfig

    def cfg(handoff: str, active: list[str]) -> TrackerConfig:
        return TrackerConfig(
            kind="github", repo="o/r", endpoint="", api_key="",
            required_labels=[], active_states=active, terminal_states=["done"],
            handoff_label=handoff)

    # prototype shape: review IS dispatched, so an agent performs the review
    assert cfg("status:review", ["todo", "in progress", "review"]).agent_owns_gate_c()
    # base shape: human-review is dispatched by nobody
    assert not cfg("status:human-review", ["triage", "todo", "in progress"]).agent_owns_gate_c()
    # label/state spelling: the hyphen-to-space normalisation must match the
    # dispatcher's, or a project reads as gated while its tickets get dispatched
    assert cfg("status:agent-review", ["todo", "agent review"]).agent_owns_gate_c()
    # a malformed label is not a licence
    assert not cfg("review", ["review"]).agent_owns_gate_c()


def test_the_shipped_stances_resolve_the_way_their_docs_claim():
    """The whole point is that civ-life keeps merging and Switchboard does not.
    Assert against the REAL templates rather than hand-built configs, so a
    stance edit that flips a project's Gate C owner fails here."""
    import re
    from orchestrator.workflow import Config, load_workflow

    repo_root = Path(__file__).resolve().parents[2]

    def tracker_for(template: Path):
        text = template.read_text(encoding="utf-8")
        # the scaffold placeholders are bound at registration; any value works
        text = re.sub(r"\{\{[A-Z_]+\}\}", "1", text)
        path = template.parent / f".probe-{template.name}"
        try:
            path.write_text(text, encoding="utf-8")
            return Config(load_workflow(path), path.parent).tracker()
        finally:
            path.unlink(missing_ok=True)

    prototype = tracker_for(repo_root / "workflow" / "stances" / "WORKFLOW.prototype.md")
    base = tracker_for(repo_root / "workflow" / "WORKFLOW.base.md")

    assert prototype.agent_owns_gate_c(), (
        "the prototype stance dispatches a QA session to status:review; if this "
        "is False its QA can no longer merge and the stance is decorative")
    assert not base.agent_owns_gate_c(), (
        "base hands off to status:human-review, which nothing dispatches; if "
        "this is True the merge guard just stopped protecting Switchboard")


# --- the grant is (agent, THIS repo), never (agent) ---------------------------
#
# codex review P1, PR #150. An agent-owned project's App installation token also
# reaches every other repo that installation covers, so a project-wide boolean
# let one autonomous project merge straight through a human-gated project's
# Gate C. `gh pr merge` names another repo two ways — `-R/--repo`, and a PR URL
# in place of the number — and both had to be closed.

@pytest.mark.parametrize("command", [
    "gh pr merge -R colin-prologue/Switchboard 12",
    "gh pr merge --repo colin-prologue/Switchboard 12",
    "gh pr merge --repo=colin-prologue/Switchboard 12",
    "gh pr merge -Rcolin-prologue/Switchboard 12",
    "gh -R colin-prologue/Switchboard pr merge 12",       # flag before `pr`
    "gh pr -R colin-prologue/Switchboard merge 12",       # flag between
    "gh pr merge 12 -R colin-prologue/Switchboard",       # flag after the arg
    "gh pr merge https://github.com/colin-prologue/Switchboard/pull/12",
    "gh pr merge HTTPS://GitHub.com/colin-prologue/Switchboard/pull/12",
])
def test_merge_into_another_repo_is_denied_even_on_an_agent_owned_gate(
    command, tmp_path
):
    """civ-life's stance grants civ-life's merge right. It does not grant
    Switchboard's, whatever the shared token can reach."""
    proc = _run_bash_gate_c_agent(command, tmp_path, repo=OWN_REPO)
    assert proc.returncode == 2, f"{command!r} escaped its project"
    assert DENIAL_PREFIX in proc.stderr


def test_an_agent_flag_with_no_repo_does_not_relax(tmp_path):
    """Nothing bounds the grant, so there is nothing to check it against."""
    proc = _run_bash_gate_c_agent("gh pr merge 12", tmp_path, repo="")
    assert proc.returncode == 2
    assert DENIAL_PREFIX in proc.stderr


@pytest.mark.parametrize("configured,named", [
    ("colin-prologue/civ-life", "Colin-Prologue/Civ-Life"),
    ("Colin-Prologue/Civ-Life", "colin-prologue/civ-life"),
])
def test_repo_comparison_is_case_insensitive(configured, named, tmp_path):
    """GitHub owner/name are case-insensitive; a case-sensitive compare would
    deny a project its own merges and read as the guard being broken."""
    proc = _run_bash_gate_c_agent(
        f"gh pr merge -R {named} 12", tmp_path, repo=configured)
    assert proc.returncode == 0, proc.stderr


def test_selector_hands_the_guard_the_repo_the_scheduler_works():
    """The two must come from the same tracker block, or the guard could be
    told a repo this project is not actually working."""
    from orchestrator.runner_selector import _gate_c_repo

    class _Cfg:
        def __init__(self, tracker):
            self._t = tracker

        def tracker(self):
            return self._t

    from orchestrator.types import TrackerConfig

    agent = TrackerConfig(
        kind="github", repo="colin-prologue/civ-life", endpoint="", api_key="",
        required_labels=[], active_states=["todo", "in progress", "review"],
        terminal_states=["closed"], handoff_label="status:review")
    human = TrackerConfig(
        kind="github", repo="colin-prologue/Switchboard", endpoint="", api_key="",
        required_labels=[], active_states=["triage", "todo", "in progress"],
        terminal_states=["closed"], handoff_label="status:human-review")

    assert _gate_c_repo(_Cfg(agent)) == "colin-prologue/civ-life"
    assert _gate_c_repo(_Cfg(human)) == ""


def test_cross_repo_denial_names_the_real_reason(tmp_path):
    """Verified live through `claude -p` (#158): the guard denies correctly, but
    told the agent "Gate C is Colin's" for a cross-repo merge on an AGENT-owned
    gate — where the gate is NOT Colin's and the project's own merges ARE
    permitted. A denial that misdescribes its cause sends a retrying session to
    the wrong fix; that is how civ-life#4 burned a full budget."""
    proc = _run_bash_gate_c_agent(
        "gh pr merge -R colin-prologue/Switchboard 12", tmp_path, repo=OWN_REPO)
    assert proc.returncode == 2
    assert "another project's repo" in proc.stderr
    assert "ITS OWN repo only" in proc.stderr
    assert "Gate C is Colin's" not in proc.stderr, (
        "the human-gate hint must not be reused for a cross-repo refusal")


def test_human_gate_denial_keeps_the_handoff_hint(tmp_path):
    """The original message is still right where the gate really is a human's."""
    proc = _run_bash(("gh pr merge 12"), tmp_path)
    assert proc.returncode == 2
    assert "Gate C is Colin's" in proc.stderr
    assert "another project's repo" not in proc.stderr
