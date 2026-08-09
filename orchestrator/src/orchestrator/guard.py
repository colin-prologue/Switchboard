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
        # Global flags may sit between `gh` and `pr`; the first non-flag token
        # must be THIS command's subcommand, never a later `pr` in prose.
        rest = tokens[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        if rest[:1] == ["pr"] and len(rest) >= 2:
            verb, args = rest[1], rest[2:]
            if verb in GH_PR_DENIED_VERBS:
                return GH_PR_DENIED_VERBS[verb]
            if verb == "review" and any(
                a == "--approve" or a == "-a"
                # bundled short flags: `-am "lgtm"` carries the approve `a`
                or (len(a) > 1 and a[0] == "-" and a[1] != "-" and "a" in a[1:])
                for a in args
            ):
                return "gh pr review --approve"
        return None

    if tokens[:2] == ["git", "push"]:
        # `git push` takes no free-text argument, so a plain per-token scan is
        # safe here (and word-boundary regexes cannot express `+refspec`).
        for tok in tokens[2:]:
            if tok == "--force" or tok.startswith("--force-with-lease"):
                return f"git push {tok}"
            if tok == "-f" or (
                # bundled short options: `-fu` carries force (codex review,
                # PR #136 — git accepts bundled shorts; whole-token equality
                # missed them)
                len(tok) > 1 and tok[0] == "-" and tok[1] != "-" and "f" in tok[1:]
            ):
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
