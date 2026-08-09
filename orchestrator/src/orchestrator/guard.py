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
                           "--exec-path")


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
                if verb == "review" and any(
                    a == "--approve" or a == "-a"
                    # explicit boolean form: --approve=true; =false is
                    # over-denied deliberately (fail toward deny — a worker
                    # has no reason to write it, and denial is soft)
                    or a.startswith("--approve=")
                    # bundled booleans, value-aware: `-am` denies, `-b=x`
                    # and `-ba` do not (b takes a value; F is --body-file)
                    or _bundled_bool(a, "a", "bF")
                    for a in args
                ):
                    return "gh pr review --approve"
        return None

    if tokens[0] == "git":
        # Git accepts global options before the subcommand (`git -C . push`,
        # `git -c k=v push` — codex review, PR #136); locate `push` as the
        # first non-flag token with global values consumed.
        rest = _skip_flags(tokens[1:], _GIT_GLOBAL_VALUE_FLAGS)
        if rest[:1] == ["push"]:
            for tok in rest[1:]:
                if tok == "--force" or tok.startswith("--force-with-lease"):
                    return f"git push {tok}"
                # value-aware bundle scan: `-fu`/`-uf` deny, `-ofoo` is the
                # push-option VALUE and does not (o takes a value)
                if tok == "-f" or _bundled_bool(tok, "f", "o"):
                    return f"git push {tok} (force)"
                if len(tok) > 1 and tok.startswith("+"):
                    return f"git push {tok} (force refspec)"
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
