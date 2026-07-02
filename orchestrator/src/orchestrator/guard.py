"""PreToolUse workspace-containment guard.

implements: spec/SPEC.md §1 binding row "sandbox / safety invariants ->
            PreToolUse hooks vetoing tool calls outside the per-issue
            workspace"; complements core §9.5 invariants at the tool layer.

Standalone script (stdlib only) injected into the agent session by runner.py
via `--settings`. Claude Code invokes it before each matched tool call with a
JSON payload on stdin; exit 2 + stderr = deny (fed back to the model), exit 0 =
allow.

v1 scope (documented): file-mutation tools (Write/Edit/NotebookEdit) are
denied when their target path resolves outside the workspace. Bash commands
are not statically analyzed in v1 — the workspace cwd, the fresh clone, and
the allowlisted git/gh commands bound the blast radius there.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FILE_PATH_KEYS = ("file_path", "notebook_path", "path")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0  # malformed hook input: do not brick the session

    workspace = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or ""
    if not workspace:
        return 0
    root = Path(workspace).resolve()

    tool_input = payload.get("tool_input") or {}
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
