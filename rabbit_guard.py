#!/usr/bin/env python3
"""rabbit_guard.py — standalone rabbit-trail guard for Claude Code.

A combined PostToolUse + Stop hook. After every tool call it runs cheap,
deterministic tripwires. When one trips, it spawns a FRESH reviewer call
(Fable) fed only a compact digest — never the polluted session context —
which returns an enumerated verdict the hook injects back to Claude.

Verdict schema — the standalone form of your orchestrated control-verdict
contract (drop `target`/`reassign`, since there is only one agent):

    {
      "verdict": "continue" | "redirect" | "rollback" | "reset_context" | "halt_for_human",
      "reason": "<one line>",
      "corrected_directive": "<what to do instead, if any>"
    }

Design rules baked in:
  * FAIL OPEN. A guard must never crash or stall the session. Any error -> exit 0.
  * CHEAP FIRST. Tripwires are pure Python. The reviewer (paid Fable call) runs
    ONLY on a trip, rate-limited by a cooldown and a hard per-session cap. If the
    review budget is spent, it still injects a free deterministic nudge.
  * MANUFACTURED OUTSIDE VIEW. The reviewer receives a digest, not the transcript.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# ---------------------------------------------------------------- config (env)
MODEL            = os.environ.get("RG_MODEL", "claude-fable-5")
EFFORT           = os.environ.get("RG_EFFORT")               # e.g. "high"; omitted if unset
GOAL_FILE        = os.environ.get("RG_GOAL_FILE", "GOAL.md")
LEDGER_FILE      = os.environ.get("RG_LEDGER_FILE", "PROGRESS.md")

REPEAT_CALL_LIMIT  = int(os.environ.get("RG_REPEAT_CALL_LIMIT", "3"))   # identical tool calls
REPEAT_ERROR_LIMIT = int(os.environ.get("RG_REPEAT_ERROR_LIMIT", "3"))  # same error signature
STALL_LIMIT        = int(os.environ.get("RG_STALL_LIMIT", "8"))         # tool calls w/o ledger change
TOOLCALL_BUDGET    = int(os.environ.get("RG_TOOLCALL_BUDGET", "120"))
WALLCLOCK_BUDGET_S = int(os.environ.get("RG_WALLCLOCK_BUDGET_S", "3600"))

REVIEW_COOLDOWN    = int(os.environ.get("RG_REVIEW_COOLDOWN", "5"))     # min tool calls between paid reviews
MAX_REVIEWS        = int(os.environ.get("RG_MAX_REVIEWS", "6"))         # hard cap per session
RECENT_WINDOW      = int(os.environ.get("RG_RECENT_WINDOW", "10"))

STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "rabbit-guard")
VALID = {"continue", "redirect", "rollback", "reset_context", "halt_for_human"}


# ------------------------------------------------------------------ utilities
def _safe_exit(payload=None):
    """Emit at most one JSON object, then exit 0. Always the final word."""
    if payload:
        sys.stdout.write(json.dumps(payload))
    sys.exit(0)


def _read(path, limit=4000):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[:limit]
    except Exception:
        return ""


def _state_path(session_id):
    return os.path.join(STATE_DIR, f"{re.sub(r'[^A-Za-z0-9_.-]', '_', session_id)}.json")


def _load_state(session_id):
    s = _read(_state_path(session_id), limit=200000)
    if s:
        try:
            return json.loads(s)
        except Exception:
            pass
    return {
        "start": time.time(), "calls": 0, "reviews": 0, "last_review_call": -999,
        "recent_calls": [], "recent_call_hashes": [], "recent_error_sigs": [],
        "ledger_hash": "", "stall": 0, "recent_moves": [],
    }


def _save_state(session_id, st):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_state_path(session_id), "w", encoding="utf-8") as f:
            json.dump(st, f)
    except Exception:
        pass


def _call_hash(tool_name, tool_input):
    blob = tool_name + "::" + json.dumps(tool_input, sort_keys=True, default=str)[:2000]
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def _error_sig(tool_name, tool_response):
    """Group similar failures: tool + normalized tail of stderr (digits/paths masked)."""
    exit_code = tool_response.get("exit_code", tool_response.get("exitCode"))
    stderr = (tool_response.get("stderr") or "")
    if not stderr and isinstance(tool_response.get("error"), str):
        stderr = tool_response["error"]
    if (exit_code in (None, 0)) and not stderr:
        return None
    tail = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit={exit_code}"
    tail = re.sub(r"0x[0-9a-fA-F]+|\d+", "#", tail)            # mask numbers
    tail = re.sub(r"(/[^\s:]+)+", "<path>", tail)              # mask paths
    return f"{tool_name}:{tail[:120]}"


def _trim(seq, n):
    return seq[-n:] if len(seq) > n else seq


# ----------------------------------------------------------------- tripwires
def evaluate_tripwires(st):
    """Return (tripped, signal, evidence) — pure Python, no model, no cost."""
    # budgets first (hard limits -> usually a human handoff)
    if st["calls"] >= TOOLCALL_BUDGET:
        return True, "tool_budget", f"{st['calls']} tool calls (budget {TOOLCALL_BUDGET})"
    if time.time() - st["start"] >= WALLCLOCK_BUDGET_S:
        mins = int((time.time() - st["start"]) / 60)
        return True, "wallclock_budget", f"{mins} min elapsed (budget {WALLCLOCK_BUDGET_S // 60} min)"

    # thrashing: identical tool call repeated within the recent window
    if st["recent_call_hashes"]:
        last = st["recent_call_hashes"][-1]
        reps = st["recent_call_hashes"].count(last)
        if reps >= REPEAT_CALL_LIMIT:
            return True, "repeated_call", f"identical tool call x{reps} in last {RECENT_WINDOW}"

    # error loop: same error signature recurring
    if st["recent_error_sigs"]:
        last = st["recent_error_sigs"][-1]
        reps = st["recent_error_sigs"].count(last)
        if reps >= REPEAT_ERROR_LIMIT:
            return True, "repeated_error", f"error '{last}' x{reps}"

    # no-progress: ledger unchanged for STALL_LIMIT consecutive tool calls
    if st["stall"] >= STALL_LIMIT:
        return True, "no_progress", f"{LEDGER_FILE} unchanged for {st['stall']} tool calls"

    return False, "", ""


# --------------------------------------------------------------- fresh review
def build_digest(st, signal, evidence):
    goal = _read(GOAL_FILE, 1500).strip() or "(no GOAL.md found — infer intent from recent moves)"
    return {
        "goal": goal,
        "tripwire": signal,
        "evidence": evidence,
        "recent_moves": st["recent_moves"][-RECENT_WINDOW:],
        "ledger_tail": _read(LEDGER_FILE, 1200).strip()[-1200:],
        "tool_calls_so_far": st["calls"],
    }


def run_reviewer(digest):
    """Fresh, context-free judge. Returns a validated verdict dict; fail-open to continue."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return {"verdict": "continue", "reason": "no API key; review skipped", "corrected_directive": ""}

    system = (
        "You are a FRESH reviewer with no prior context on this coding session. "
        "You are handed a digest of a session that tripped a stall detector. Your job is to "
        "judge from the outside whether it is on a productive path or stuck in a rabbit trail, "
        "and issue exactly one control verdict. Be decisive and brief. "
        "Respond with ONLY a JSON object, no prose, no markdown fences:\n"
        '{"verdict":"continue|redirect|rollback|reset_context|halt_for_human",'
        '"reason":"<=20 words","corrected_directive":"<concrete next instruction, or empty if continue>"}\n'
        "Guidance: continue = false alarm, on track. redirect = wrong approach, give the right one. "
        "rollback = recent work is corrupt, revert to last good commit then do X. "
        "reset_context = the trail is context rot, recommend a fresh session with the goal restated. "
        "halt_for_human = ambiguous or risky enough that a person must decide."
    )
    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": "DIGEST:\n" + json.dumps(digest, indent=2)}],
    }
    if EFFORT and MODEL.startswith(("claude-fable", "claude-mythos")):
        body["effort"] = EFFORT  # placement may evolve; we retry without it on 400

    def _post(payload):
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())

    try:
        try:
            data = _post(body)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "effort" in body:   # unknown param -> retry clean
                body.pop("effort", None)
                data = _post(body)
            else:
                raise
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        v = json.loads(text)
        if v.get("verdict") not in VALID:
            return {"verdict": "continue", "reason": "unparseable verdict", "corrected_directive": ""}
        v.setdefault("reason", "")
        v.setdefault("corrected_directive", "")
        return v
    except Exception as e:
        # never let the judge brick the session
        return {"verdict": "continue", "reason": f"reviewer error: {type(e).__name__}",
                "corrected_directive": ""}


# ------------------------------------------------------------- output mapping
def emit_post_tool(signal, verdict):
    msg = f"[rabbit-guard:{signal}] {verdict['reason']}".strip()
    directive = verdict.get("corrected_directive", "").strip()
    v = verdict["verdict"]

    if v == "continue":
        _safe_exit()  # silent; let it proceed
    if v == "halt_for_human":
        _safe_exit({"continue": False, "stopReason": msg + " — handing off to you."})

    if v == "rollback":
        reason = f"{msg}\nRevert to the last good commit, then: {directive}"
    elif v == "reset_context":
        reason = (f"{msg}\nThis looks like context rot. Restate the goal from {GOAL_FILE} into a "
                  f"fresh session and abandon this context. {directive}")
    else:  # redirect
        reason = f"{msg}\nCorrected direction: {directive}"
    # On PostToolUse, decision:block injects `reason` to Claude as automated feedback.
    _safe_exit({"decision": "block", "reason": reason})


def emit_stop(signal, verdict, stop_hook_active):
    v = verdict["verdict"]
    # Allow the turn to end if we're already looping, told to continue, or escalating to a human.
    if stop_hook_active or v in ("continue", "halt_for_human"):
        _safe_exit()
    directive = verdict.get("corrected_directive", "").strip()
    reason = f"[rabbit-guard:{signal}] {verdict['reason']}\nDo not stop yet: {directive}"
    _safe_exit({"decision": "block", "reason": reason})


# -------------------------------------------------------------------- driver
def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        _safe_exit()

    session_id = str(data.get("session_id") or "default")
    event = data.get("hook_event_name") or data.get("hook_event") or ""
    is_stop = event == "Stop" or ("stop_hook_active" in data and "tool_name" not in data)
    st = _load_state(session_id)

    # --- Stop event: completion / no-progress guard -------------------------
    if is_stop:
        tripped, signal, evidence = evaluate_tripwires(st)
        if not tripped:
            _safe_exit()
        verdict = (run_reviewer(build_digest(st, signal, evidence))
                   if st["reviews"] < MAX_REVIEWS else
                   {"verdict": "halt_for_human", "reason": "review cap reached", "corrected_directive": ""})
        st["reviews"] += 1
        _save_state(session_id, st)
        emit_stop(signal, verdict, bool(data.get("stop_hook_active")))

    # --- PostToolUse: per-tool-call tripwire layer --------------------------
    tool_name = data.get("tool_name") or "?"
    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response") or data.get("tool_result") or {}

    st["calls"] += 1

    # update rolling deterministic state
    h = _call_hash(tool_name, tool_input)
    st["recent_call_hashes"] = _trim(st["recent_call_hashes"] + [h], RECENT_WINDOW)
    sig = _error_sig(tool_name, tool_response)
    if sig:
        st["recent_error_sigs"] = _trim(st["recent_error_sigs"] + [sig], RECENT_WINDOW)
    ledger_hash = hashlib.sha1(_read(LEDGER_FILE, 8000).encode("utf-8", "replace")).hexdigest()
    st["stall"] = 0 if ledger_hash != st["ledger_hash"] else st["stall"] + 1
    st["ledger_hash"] = ledger_hash
    st["recent_moves"] = _trim(
        st["recent_moves"] + [{
            "tool": tool_name,
            "input": json.dumps(tool_input, default=str)[:160],
            "ok": sig is None,
        }], RECENT_WINDOW)

    tripped, signal, evidence = evaluate_tripwires(st)
    if not tripped:
        _save_state(session_id, st)
        _safe_exit()

    # trip: spend a paid review only if cooldown + cap allow; else free nudge
    can_review = (st["calls"] - st["last_review_call"] >= REVIEW_COOLDOWN) and (st["reviews"] < MAX_REVIEWS)
    if can_review:
        verdict = run_reviewer(build_digest(st, signal, evidence))
        st["reviews"] += 1
        st["last_review_call"] = st["calls"]
        st["stall"] = 0  # reset so we don't re-fire every call after a verdict
        _save_state(session_id, st)
        emit_post_tool(signal, verdict)
    else:
        _save_state(session_id, st)
        # free deterministic nudge (no model call) — additionalContext, non-blocking
        _safe_exit({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"[rabbit-guard:{signal}] {evidence}. Pause and reassess before repeating.",
        }})


if __name__ == "__main__":
    try:
        main()
    except Exception:
        _safe_exit()  # fail open, always
