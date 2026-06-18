#!/usr/bin/env python3
"""Deterministic tripwire guard (rabbit_guard v2, sub-plan B §3). Token-free —
NO model calls, NO API key (the v1 paid reviewer is deleted; its judgment role
moved to A's verification lane). Wired as BOTH PreToolUse and PostToolUse (one
script, dispatch on hook_event_name). Per-subagent state keyed by agent_id.

Two-strike: 1st trip -> PostToolUse nudge; 2nd trip -> next PreToolUse denies and
directs the subagent to stop (A's worker then synthesizes a `blocked` result via
sb block). Fail open always: any error -> exit 0.
"""
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_RECENT_WINDOW = 10


def new_state(now):
    return {"start": now, "calls": 0, "since_progress": 0,
            "recent_call_hashes": [], "recent_error_sigs": [],
            "trips": 0, "nudges": 0, "last_nudge_call": -999,
            "block_armed": False}


def _call_hash(tool_name, tool_input):
    blob = tool_name + "::" + json.dumps(tool_input, sort_keys=True, default=str)[:2000]
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def _error_sig(tool_name, text):
    """Group similar failures: tool + normalized tail (digits/paths masked)."""
    if not text:
        return None
    low = text.lower()
    if not any(k in low for k in ("error", "traceback", "exception", "failed",
                                  "fatal", "no such")):
        return None
    tail = text.strip().splitlines()[-1] if text.strip() else ""
    tail = re.sub(r"0x[0-9a-fA-F]+|\d+", "#", tail)
    tail = re.sub(r"(/[^\s:]+)+", "<path>", tail)
    return f"{tool_name}:{tail[:120]}"


def _is_progress(tool_name, tool_input):
    if tool_name in _EDIT_TOOLS:
        return True
    if tool_name == "Bash" and "git commit" in str(tool_input.get("command", "")):
        return True
    path = str(tool_input.get("file_path", ""))
    return "/results/" in path or path.endswith(".switchboard")


def _trim(seq, n):
    return seq[-n:] if len(seq) > n else seq


def update_state(state, payload, cfg):
    """Fold one PostToolUse call into the rolling per-agent ledger. Pure."""
    tool_name = payload.get("tool_name") or "?"
    tool_input = payload.get("tool_input") or {}
    resp = payload.get("tool_response") or {}
    text = resp.get("text") if isinstance(resp, dict) else None

    state["calls"] += 1
    state["recent_call_hashes"] = _trim(
        state["recent_call_hashes"] + [_call_hash(tool_name, tool_input)],
        _RECENT_WINDOW)
    sig = _error_sig(tool_name, text)
    if sig:
        state["recent_error_sigs"] = _trim(
            state["recent_error_sigs"] + [sig], _RECENT_WINDOW)
    state["since_progress"] = (0 if _is_progress(tool_name, tool_input)
                               else state["since_progress"] + 1)
    return state


def evaluate(state, cfg, now=None):
    """Return (tripped, signal, evidence). Pure, no cost. Order: budgets,
    repeat-call, repeat-error, no-progress (matches rabbit_guard precedence)."""
    now = time.time() if now is None else now
    if state["calls"] >= cfg["guard_max_tool_calls"]:
        return True, "tool_budget", f"{state['calls']} calls (budget {cfg['guard_max_tool_calls']})"
    if now - state["start"] >= cfg["guard_max_wall_s"]:
        return True, "wallclock_budget", f"{int(now - state['start'])}s elapsed"
    h = state["recent_call_hashes"]
    if h and h.count(h[-1]) >= cfg["guard_repeat_call"]:
        return True, "repeat_call", f"identical call x{h.count(h[-1])} in last {_RECENT_WINDOW}"
    e = state["recent_error_sigs"]
    if e and e.count(e[-1]) >= cfg["guard_repeat_error"]:
        return True, "repeat_error", f"error '{e[-1]}' x{e.count(e[-1])}"
    if state["since_progress"] >= cfg["guard_no_progress"]:
        return True, "no_progress", f"{state['since_progress']} calls since progress"
    return False, "", ""
