#!/usr/bin/env python3
"""Token-free rate-limit / usage-cap detector (sub-plan B §4, HDR-011).

PostToolUse hook: inspect tool_response.text for rate-limit signal strings (pure
regex — works even when the session has no tokens left). On match, write
.switchboard/quota.json. DETECTION ONLY: the worker loop reads it advisory to
tune backoff; nothing ever gates a claim on it (a shared throttle hits the whole
fleet at once, HDR-009). Fail open: any error -> exit 0, never stall a session.
"""
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EXHAUSTED = re.compile(
    r"usage (limit|cap)|reached your (usage|limit)|quota exceeded|"
    r"insufficient quota|out of (credits|quota)", re.I)
_THROTTLED = re.compile(r"\b429\b|rate.?limit|too many requests|overloaded", re.I)


def detect(text):
    """Return {state, detail} or None. Exhausted checked first (more specific)."""
    if not text:
        return None
    if _EXHAUSTED.search(text):
        return {"state": "exhausted", "detail": text[:200]}
    if _THROTTLED.search(text):
        return {"state": "throttled", "detail": text[:200]}
    return None


# Containment cap (director review 2026-06-19): a missing/misconfigured root must
# not send the upward search climbing to the filesystem root or into a different
# project. The hook cwd is the repo root (0 levels up) or a task worktree at
# <repo>/.worktrees/<id> (2 levels up), so this bound covers every legitimate
# case with wide margin while keeping the walk inside the project.
_MAX_UP = 16


def find_root(cwd, max_up=_MAX_UP):
    """Walk up from cwd to the dir containing .switchboard/ — bounded to at most
    `max_up` parent levels (containment, ADR-002). The subagent cwd is its
    worktree, which has no .switchboard. None if not found within the bound."""
    d = os.path.abspath(cwd or ".")
    for _ in range(max_up + 1):
        if os.path.isdir(os.path.join(d, ".switchboard")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def run(payload):
    resp = payload.get("tool_response") or {}
    text = resp.get("text") if isinstance(resp, dict) else None
    state = detect(text)
    if not state:
        return
    root = find_root(payload.get("cwd"))
    if not root:
        return
    state["at"] = time.time()
    qp = os.path.join(root, ".switchboard", "quota.json")
    tmp = f"{qp}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, qp)


def main():
    try:
        run(json.load(sys.stdin))
    except Exception:
        pass
    sys.exit(0)  # fail open, always


if __name__ == "__main__":
    main()
