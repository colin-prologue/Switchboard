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


def _nudge(signal, evidence):
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (f"[sb-guard:{signal}] {evidence}. You appear to be "
                              f"looping without progress — stop and reassess; do "
                              f"not repeat the same action.")}}


def decide_post(state, cfg, now=None):
    """Returns (output_dict_or_None, new_state). Two-strike + fail-safe (ADR-003):
    trip #1 nudges (cooldown + cap respected); the block arms on trip #2 OR when
    the nudge budget is exhausted — so a subagent that keeps tripping past its
    nudge budget escalates to a hard block instead of getting silence."""
    tripped, signal, evidence = evaluate(state, cfg, now=now)
    if not tripped:
        return None, state
    state["trips"] += 1
    nudge_budget_left = state["nudges"] < cfg["guard_nudge_cap"]
    cooldown_ok = state["calls"] - state["last_nudge_call"] >= cfg["guard_cooldown_calls"]
    if state["trips"] >= 2 or not nudge_budget_left:
        state["block_armed"] = True
    if nudge_budget_left and cooldown_ok:
        state["nudges"] += 1
        state["last_nudge_call"] = state["calls"]
        return _nudge(signal, evidence), state
    return None, state


def decide_pre(state):
    """Returns a deny output if a 2nd strike armed the block, else None."""
    if not state.get("block_armed"):
        return None
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "[sb-guard] Repeated no-progress / looping detected. Stop now: do not "
            "call more tools. Write a brief `blocked` result if you can, then end "
            "your turn — the worker will file a blocked result for human review.")}}


def _state_path(repo, agent_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", agent_id)
    return os.path.join(repo, ".switchboard", "guard", f"{safe}.json")


def load_state(repo, agent_id, now):
    p = _state_path(repo, agent_id)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return new_state(now)


def save_state(repo, agent_id, state):
    p = _state_path(repo, agent_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = f"{p}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, p)


def _emit(payload):
    if payload:
        sys.stdout.write(json.dumps(payload))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
        agent_id = data.get("agent_id")
        if not agent_id:          # top-level worker-session call: never guarded
            _emit(None)
        from sb import paths
        root = sb_quota_find_root(data.get("cwd"))
        if not root:
            _emit(None)
        cfg = paths.load_config(paths.Layout(root))
        event = data.get("hook_event_name") or ""
        now = time.time()
        st = load_state(root, agent_id, now)
        if event == "PreToolUse":
            out = decide_pre(st)
            _emit(out)
        # PostToolUse
        st = update_state(st, data, cfg)
        out, st = decide_post(st, cfg, now=now)
        save_state(root, agent_id, st)
        _emit(out)
    except SystemExit:
        raise
    except Exception:
        sys.exit(0)   # fail open, always


def sb_quota_find_root(cwd):
    """Same upward .switchboard discovery as the quota hook (ADR-002)."""
    from hooks.sb_quota import find_root
    return find_root(cwd)


if __name__ == "__main__":
    main()
