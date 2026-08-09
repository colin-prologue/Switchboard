"""PreToolUse workspace-containment guard.

implements: spec/SPEC.md §1 binding row "sandbox / safety invariants ->
            PreToolUse hooks vetoing tool calls outside the per-issue
            workspace"; complements core §9.5 invariants at the tool layer.

Standalone script (stdlib only) injected into the agent session by runner.py
via `--settings`. Claude Code invokes it before each matched tool call with a
JSON payload on stdin; exit 2 + stderr = deny (fed back to the model), exit 0 =
allow.

Scope (documented), two independent rules:

1. Containment: file-mutation tools (Write/Edit/MultiEdit/NotebookEdit) are
   denied when their target path resolves outside the workspace.
2. Merge guard (issue #133, AgDR-036): Bash calls whose command matches one of
   an ENUMERATED set of Gate-C-violating verb shapes (`gh pr merge`,
   `gh pr review … --approve`, `gh pr close`, force-pushes) are denied. This is
   a pinned enumeration, NOT general Bash static analysis and NOT a security
   boundary: denial semantics are soft (AgDR-004's addendum — denials are fed
   back to the agent, which may route around them), and `gh api -X PUT
   repos/{o}/{r}/pulls/{n}/merge` reaches the same endpoint with the same
   inherited token. It raises the cost of a Gate C violation and makes every
   attempt observable in the transcript; branch protection is the mechanical
   backstop for the residual. Everything else about Bash is still unanalyzed —
   the workspace cwd, the fresh clone, and the allowlisted git/gh commands
   bound the blast radius there.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from pathlib import Path

FILE_PATH_KEYS = ("file_path", "notebook_path", "path")

HANDOFF_HINT = "(Gate C is Colin's — hand off, don't self-merge)"

# `gh pr <verb>` shapes denied outright, verb -> human name of the shape.
GH_PR_DENIED_VERBS = {
    "merge": "gh pr merge",
    "close": "gh pr close",
}


def _tokenize(command: str) -> list[str]:
    """shlex so a quoted `--body "gh pr merge here"` collapses to ONE token (no
    bare `pr` inside prose) and `+refs/heads/x` stays one token. An unbalanced
    quote falls back to a naive split — fail toward evaluation, never toward a
    silent allow."""
    # bash removes backslash-newline before parsing, but shlex leaves the
    # newline glued to the next token (`gh \<NL>pr merge` -> "\npr" — codex
    # review r9, PR #136); match bash before tokenizing
    command = command.replace("\\\n", "")
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _bundled_bool(token: str, target: str, value_takers: str) -> bool:
    """Whether a short-flag bundle carries boolean flag `target`.

    pflag/git parse bundles left-to-right: booleans may stack until the first
    value-taking flag, whose remaining chars (or `=…` suffix) are its VALUE,
    not flags (codex review, PR #136 — `-b=thanks` and `-ofoo` are values and
    must not be scanned for `a`/`f`). Scan stops at `=` or the first
    value-taker.
    """
    if len(token) < 2 or token[0] != "-" or token[1] == "-":
        return False
    for ch in token[1:]:
        if ch == "=" or ch in value_takers:
            return False if ch != target else True
        if ch == target:
            return True
    return False


def _skip_flags(tokens: list[str], value_takers: tuple[str, ...]) -> list[str]:
    """Drop leading flags AND the values of flags that take one.

    `-R o/r` / `--repo o/r` consume the following token; `--repo=o/r` and
    `-Ro/r` are self-contained (codex review, PR #136 — leaving a flag's
    VALUE in subcommand position let `gh -R o/r pr merge` bypass the deny).
    """
    rest = list(tokens)
    while rest and rest[0].startswith("-"):
        flag = rest[0]
        rest = rest[1:]
        bare = flag.split("=", 1)[0]
        if "=" not in flag and bare in value_takers and rest:
            # attached short values (`-Ro/r`) are already self-contained:
            # only a BARE value-taking flag consumes the next token.
            if bare.startswith("--") or len(flag) == 2:
                rest = rest[1:]
    return rest


_GH_VALUE_FLAGS = ("-R", "--repo")
_GIT_GLOBAL_VALUE_FLAGS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                           "--exec-path", "--config-env")


def _denied_shape(command: str) -> str | None:
    """Name the denied shape this command matches, or None.

    Matching is by VERB POSITION, not token presence: the rules are anchored at
    `tokens[0]`, so free-text arguments (a PR body that says "a human will
    merge") never match. Compound commands are deliberately not split — a NAMED
    residual, consistent with the raises-the-cost-not-a-boundary posture.
    """
    tokens = _tokenize(command)
    if not tokens:
        return None

    if tokens[0] == "gh":
        # Flags (with their values) may sit between `gh` and `pr` AND between
        # `pr` and the verb (`gh pr -R o/r merge` — codex review, PR #136);
        # the verb is the first non-flag token after the `pr` subcommand.
        rest = _skip_flags(tokens[1:], _GH_VALUE_FLAGS)
        if rest[:1] == ["pr"]:
            rest = _skip_flags(rest[1:], _GH_VALUE_FLAGS)
            if rest:
                verb, args = rest[0], rest[1:]
                if verb in GH_PR_DENIED_VERBS:
                    return GH_PR_DENIED_VERBS[verb]
                if verb == "review":
                    i = 0
                    while i < len(args):
                        a = args[i]
                        # bare body/body-file consume their SEPARATED value
                        # (codex review r4, PR #136: `--body --approve` makes
                        # `--approve` the literal body text, not an approval)
                        if a in ("-b", "--body", "-F", "--body-file",
                                 "-R", "--repo"):
                            i += 2
                            continue
                        if (
                            a == "--approve" or a == "-a"
                            # explicit boolean form: --approve=true; =false is
                            # over-denied deliberately (fail toward deny — a
                            # worker has no reason to write it; denial is soft)
                            or a.startswith("--approve=")
                            # bundled booleans, value-aware: `-am` denies,
                            # `-b=x`/`-ba` do not (b/F take values)
                            # R takes a value too: -Ro/a's `a` is part of
                            # the repo selector, not an approve (r6)
                            or _bundled_bool(a, "a", "bFR")
                        ):
                            return "gh pr review --approve"
                        i += 1
        return None

    if tokens[0] == "git":
        # Git accepts global options before the subcommand (`git -C . push`,
        # `git -c k=v push` — codex review, PR #136); locate `push` as the
        # first non-flag token with global values consumed. While consuming,
        # inspect -c / --config-env values: a `remote.<name>.push=+refspec`
        # config IS a force push smuggled through a global flag (codex
        # review r6 demonstrated it rewinding a remote), and a
        # --config-env indirection hides the value in an env var we cannot
        # read. Push-config denials only apply when the located subcommand
        # is actually `push` (codex review r9 — `git -c ... status` is
        # read-only); alias configs deny unconditionally, since an alias
        # renames a verb out from under the subcommand match entirely.
        push_cfg_denial = None
        gi = 1
        while gi < len(tokens) and tokens[gi].startswith("-"):
            flag = tokens[gi]
            source = None
            value = None
            if flag.startswith("-c") and not flag.startswith("--") and len(flag) > 2:
                # attached short form: -cremote.origin.push=+x
                source, value = "-c", flag[2:]
            elif flag == "-c" or flag.split("=", 1)[0] == "--config-env":
                source = flag.split("=", 1)[0]
                if "=" in flag and source == "--config-env":
                    value = flag.split("=", 1)[1]
                elif gi + 1 < len(tokens):
                    # bare form: the value is the next token (`-c k=v`)
                    value = tokens[gi + 1]
                    gi += 1
            if value is not None:
                key = value.split("=", 1)[0]
                val = value.split("=", 1)[1] if "=" in value else ""
                # config variable names are case-insensitive
                # (`remote.origin.Push` works — codex review r7, PR #136)
                key_cf = key.lower()
                if key_cf.startswith("alias."):
                    return f"git -c {key}=... (alias defined via config)"
                if key_cf.endswith(".push") and (
                    val.startswith("+") or source == "--config-env"
                ):
                    push_cfg_denial = f"git -c {key}=+... (force via push config)"
                # remote.<name>.mirror=true is `push --mirror` by config
                # (codex review r7 demonstrated it deleting remote refs);
                # deny for any value — no legitimate worker sets it, and a
                # --config-env value is unreadable here anyway
                if key_cf.startswith("remote.") and key_cf.endswith(".mirror"):
                    push_cfg_denial = f"git -c {key}=... (mirror via remote config)"
            gi += 1
        rest = _skip_flags(tokens[1:], _GIT_GLOBAL_VALUE_FLAGS)
        if push_cfg_denial and rest[:1] == ["push"]:
            return push_cfg_denial
        if rest[:1] == ["push"]:
            args = rest[1:]
            i = 0
            positionals = 0
            while i < len(args):
                tok = args[i]
                # a bare -o/--push-option consumes the NEXT token as its
                # value (codex review, PR #136 — `git push -o +foo` is a
                # push option, not a force refspec)
                if tok in ("-o", "--push-option"):
                    i += 2
                    continue
                # any --force* prefix: covers --force, --force-with-lease
                # (incl. its `=<ref>:<expect>` form), --force-if-includes,
                # and git's UNAMBIGUOUS ABBREVIATIONS (`--force-w`,
                # `--force-with-l` — codex review r4 demonstrated git 2.43
                # accepts them and forces); shorter prefixes like `--forc`
                # are ambiguous across the three --force* options and git
                # rejects them, so no bypass exists below this prefix
                if tok.startswith("--force"):
                    return f"git push {tok}"
                # --mirror force-updates (and deletes) every ref; git accepts
                # unambiguous abbreviations down to `--m` (codex review r8,
                # PR #136 — no other push flag starts with `m`)
                if tok != "--" and len(tok) >= 3 and "--mirror".startswith(tok):
                    return f"git push {tok} (mirror)"
                # value-aware bundle scan: `-fu`/`-uf` deny, `-ofoo` is the
                # push-option VALUE and does not (o takes a value)
                if tok == "-f" or _bundled_bool(tok, "f", "o"):
                    return f"git push {tok} (force)"
                if not tok.startswith("-"):
                    positionals += 1
                    # the FIRST positional is the <repository> operand
                    # (codex review r5, PR #136: `git push [<options>]
                    # [<repository> [<refspec>...]]` — a remote named
                    # `+remote` is not a refspec); only subsequent
                    # positionals are refspecs. A force refspec with no
                    # repository operand is not a thing git accepts, so
                    # this does not weaken the deny.
                    if positionals >= 2 and len(tok) > 1 and tok.startswith("+"):
                        return f"git push {tok} (force refspec)"
                i += 1
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # malformed hook input: do not brick the session

    tool_input = payload.get("tool_input") or {}

    # Merge guard FIRST: it is workspace-INDEPENDENT and must not ride behind
    # the early return below, or a payload without CLAUDE_PROJECT_DIR/cwd would
    # silently allow `gh pr merge`.
    if payload.get("tool_name") == "Bash":
        shape = _denied_shape(tool_input.get("command") or "")
        if shape:
            sys.stderr.write(
                f"switchboard-guard: denied: {shape} {HANDOFF_HINT}"
            )
            return 2
        # else: fall through — the containment loop below is a no-op on Bash
        # (no file-path keys in its tool_input).

    workspace = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    if not workspace:
        return 0
    root = Path(workspace).resolve()

    for key in FILE_PATH_KEYS:
        raw = tool_input.get(key)
        if not raw:
            continue
        target = Path(raw)
        if not target.is_absolute():
            target = root / target
        target = target.resolve()
        if target != root and root not in target.parents:
            sys.stderr.write(
                f"denied: {payload.get('tool_name')} target {target} is outside "
                f"the per-issue workspace {root}. Work only inside the workspace."
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
