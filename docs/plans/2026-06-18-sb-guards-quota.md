# sb Guards + Quota/Liveness Implementation Plan (M0, Plan 3 sub-plan B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the token-free safety + observability layer — the deterministic tripwire guard (rabbit_guard v2), the rate-limit quota detector, the external liveness/quota/silent-death monitor, and the early no-progress churn detector — per the finalized spec (`docs/specs/2026-06-16-sb-guards-quota-design.md`).

**Architecture:** All deterministic, **no model calls, no API key** (the v1 paid Fable reviewer is deleted; its judgment role lives in A's verification lane). The guard + quota detector are Claude Code hooks (`hooks/sb_guard.py`, `hooks/sb_quota.py`) whose *logic* lives in importable functions so it is fully unit-testable with synthetic payloads; the hook `main()` is a thin stdin/stdout shim. The monitor (`hooks/sb_monitor.py`) is a token-free scheduled wrapper over already-built `sb` verbs (`status --emit`, `notify`) plus a churn check that reads A's loop-ledger. `hooks/` becomes a tested package; the `sb` engine is untouched except two additive config keys and a `Layout.guard` path attribute (see ADR-001).

**Tech Stack:** Python 3.11+, `jsonschema`, `pytest`. No new dependencies. Hooks read JSON on stdin and emit the Claude Code hook I/O contract (verified — see "Hook contract" below).

**Governing artifacts:** spec §1–§9; HDR-011 (quota advisory, never a claim gate); PHI-030 (verification before autonomy); the finalized A↔B contracts (deny→blocked via `sb block`, already shipped; early-churn reads A's real ledger). Decisions recorded this run: **ADR-001** (guard logic in `hooks/` package, not `sb/`), **ADR-002** (`.switchboard/` upward discovery from a worktree cwd).

---

## Background the implementer needs

You are adding a hook layer to a fully-tested engine (Plans 1+2+3-A, 140 tests green via `.venv/bin/pytest -q`). The engine is git-free and deterministic; B adds **no engine behavior**, only hooks + a monitor that *consume* engine primitives.

**Engine primitives you reuse (import, do not reinvent):**
- `sb.paths` — `Layout(repo)` (attrs `.repo .root .tasks .leases .heartbeats .results .config_path .decisions .plans`, method `.lane`); `init(repo)`; `load_config(lay) -> dict` (merges `DEFAULT_CONFIG`). **You add `Layout.guard` and two config keys in Task 1.**
- `sb.store` — `read_json(path)`, `write_json(path, obj)` (atomic via tmp+`os.replace`), `fname(id)`.
- `sb.channels` — `resolve(name) -> send(title, body)`; names `macos|stdout|null` (read `sb/channels.py` for exact names). The monitor sends churn alerts through a resolved channel — token-free (a desktop notification, no model).
- `sb.loopledger` — A's token-free ledger helper (`append`, `diagnose`, `_read`). **You add `consecutive_no_progress` in Task 2.** Ledger line: `{i, claimed_id, type, outcome, released, wall_s}`; idle waits are NOT logged; a task reaches done only via a verify-pass line, so `outcome == "done"` is the progress marker.
- `sb.cli.main(argv) -> int` — the monitor calls `main(["status","--emit"])` and `main(["notify"])`.

**Hook contract (verified against current Claude Code docs — use these exact field names):**

Input (stdin JSON), common to PreToolUse + PostToolUse:
- `hook_event_name` (`"PreToolUse"`/`"PostToolUse"`/`"SubagentStop"`), `session_id`, `cwd`, `tool_name`, `tool_input` (object).
- PostToolUse adds `tool_response` — an object `{type, text}` (the rate-limit signal lives in `.text`, NOT in stderr/exit_code — this differs from v1 `rabbit_guard.py`).
- **Subagent attribution:** `agent_id` and `agent_type` are present **only when the call is inside a Task-tool subagent**, and **absent entirely** for the top-level (worker-session) call. This is the per-subagent state key. `agent_id` absent ⇒ the call is the worker loop itself ⇒ the guard is a **no-op** (we never guard the loop, only task subagents).

Output to **DENY** a tool (PreToolUse):
```json
{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "<text shown to the model>"}}
```
Output to **nudge** without blocking (PostToolUse):
```json
{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "<corrective text>"}}
```
**Fail open, always:** any exception, malformed payload, or missing state ⇒ print nothing, exit 0. Never crash or stall a session (v1 discipline; mirror `rabbit_guard.py`'s `_safe_exit`).

**`.switchboard/` discovery (ADR-002):** a task subagent's `cwd` is its **worktree** (`<repo>/.worktrees/<worker_id>`), which has no `.switchboard/` (it is gitignored, lives only in the main working tree). So the guard/quota hooks locate the engine root by walking **up** from `cwd` until a directory containing `.switchboard/` is found. If none is found, fail open (no-op).

**Reference, not a template:** `rabbit_guard.py` (repo root) is the v1 guard — read it for the tripwire shapes, rolling-state pattern, `_call_hash`/`_error_sig`, and the fail-open `_safe_exit`. B keeps those shapes but: (a) keys state by `agent_id` not `session_id`; (b) deletes the paid reviewer entirely; (c) replaces it with the two-strike nudge→deny mechanism; (d) reads `tool_response.text` for rate-limit strings. `rabbit_guard.py` is **deleted** in Task 7.

**Testability pattern:** put pure logic in importable functions that take explicit args (payload dict, state dict, config dict) and return decisions/new-state; keep `main()` a thin shim doing only stdin parse → call → stdout. Tests import the pure functions and feed synthetic payloads — no real Claude Code, no model, deterministic.

---

## File structure

```
hooks/__init__.py                              # NEW: make hooks an importable package (tests do `from hooks import ...`)
hooks/sb_guard.py                              # NEW: tripwire guard — pure logic + thin main() (Pre+PostToolUse)
hooks/sb_quota.py                              # NEW: rate-limit detector — pure logic + thin main() (PostToolUse)
hooks/sb_monitor.py                            # NEW: token-free monitor — status --emit + notify + churn check
hooks/com.switchboard.monitor.plist.example    # NEW: launchd scheduling example (cron alt documented inline)
sb/paths.py                                    # MODIFY: add Layout.guard attr + guard/quota config keys
sb/loopledger.py                               # MODIFY: add consecutive_no_progress()
hooks/settings.example.json                    # MODIFY: wire sb_guard + sb_quota; drop rabbit_guard + API-key env
rabbit_guard.py                                # DELETE: v1 leftover, fully replaced
tests/test_guard.py                            # NEW
tests/test_quota.py                            # NEW
tests/test_monitor.py                          # NEW
tests/test_loopledger.py                       # MODIFY: add consecutive_no_progress cases
CLAUDE.md, docs/ROADMAP.md                     # MODIFY: mark B implemented
```

Convention notes:
- Guard/quota logic in `hooks/` (a package), NOT `sb/` — keeps the engine surface untouched per spec §2 while staying testable (ADR-001). Hooks import `sb.paths`/`sb.store`/`sb.channels` read-only.
- Config keys are flat (matching the flat `DEFAULT_CONFIG`): `guard_max_tool_calls`, `guard_max_wall_s`, `guard_repeat_call`, `guard_repeat_error`, `guard_no_progress`, `guard_nudge_cap`, `guard_cooldown_calls`, `monitor_churn_threshold`. All are tunable first guesses (spec §7 revision condition).
- The guard does NOT clean up per-agent state on SubagentStop in M0 (YAGNI — files are ~200 B, transient, wiped with `.switchboard/`). If `guard/` growth proves real in a long run, add a SubagentStop cleanup branch. Noted, not built.

---

### Task 1: Config keys + guard path (engine touch-points)

**Files:**
- Modify: `sb/paths.py` (`DEFAULT_CONFIG` keys + `Layout.guard`)
- Test: `tests/test_paths.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paths.py`:
```python
def test_guard_dir_and_config_defaults(lay):
    from sb import paths
    assert lay.guard.endswith("/.switchboard/guard")
    cfg = paths.load_config(lay)
    assert cfg["guard_max_tool_calls"] == 80
    assert cfg["guard_max_wall_s"] == 1200
    assert cfg["guard_repeat_call"] == 3
    assert cfg["guard_repeat_error"] == 3
    assert cfg["guard_no_progress"] == 15
    assert cfg["guard_nudge_cap"] == 3
    assert cfg["guard_cooldown_calls"] == 3
    assert cfg["monitor_churn_threshold"] == 6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_paths.py::test_guard_dir_and_config_defaults -v`
Expected: FAIL (`AttributeError: 'Layout' object has no attribute 'guard'`)

- [ ] **Step 3: Implement**

In `sb/paths.py`, add the keys to `DEFAULT_CONFIG`:
```python
DEFAULT_CONFIG = {
    "schema_version": "0.1.0",
    "verifier_tier": "sonnet",
    "verifier_tier_fallback": "opus",
    "max_attempts": 3,
    "lease_ttl_s": 5400,
    "max_chain_depth": 3,
    # sub-plan B guard/monitor thresholds (all tunable — spec §7)
    "guard_max_tool_calls": 80,
    "guard_max_wall_s": 1200,
    "guard_repeat_call": 3,
    "guard_repeat_error": 3,
    "guard_no_progress": 15,
    "guard_nudge_cap": 3,
    "guard_cooldown_calls": 3,
    "monitor_churn_threshold": 6,
}
```
And add the `guard` attribute in `Layout.__init__` (next to `self.results`):
```python
        self.guard = os.path.join(self.root, "guard")
```
Note: `init()` does not need to pre-create `guard/`; the guard hook creates it on demand (`os.makedirs(lay.guard, exist_ok=True)`), so a repo initialized before B still works.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_paths.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sb/paths.py tests/test_paths.py
git commit -m "feat(sb): guard/monitor config keys + Layout.guard path (sub-plan B)"
```

---

### Task 2: `consecutive_no_progress` in loopledger

**Files:**
- Modify: `sb/loopledger.py` (add function)
- Test: `tests/test_loopledger.py` (append)

The early-churn detector (spec §6): count the **trailing** run of ledger lines with `outcome != "done"`. A `done` line (a verify pass that promoted a target) resets the run to 0. Idle waits are not logged, so this counts task iterations only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loopledger.py`:
```python
def test_consecutive_no_progress_counts_trailing_non_done(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=1.0)
    loopledger.append(led, i=1, claimed_id="P/PH/T-2", type="task",
                      outcome="released", released=True, wall_s=1.0)
    loopledger.append(led, i=2, claimed_id="P/PH/T-2", type="task",
                      outcome="queued", released=False, wall_s=1.0)
    loopledger.append(led, i=3, claimed_id="P/PH/T-3", type="task",
                      outcome="paused", released=False, wall_s=1.0)
    assert loopledger.consecutive_no_progress(led) == 3


def test_consecutive_no_progress_resets_on_trailing_done(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id="P/PH/T-2", type="task",
                      outcome="released", released=True, wall_s=1.0)
    loopledger.append(led, i=1, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=1.0)
    assert loopledger.consecutive_no_progress(led) == 0


def test_consecutive_no_progress_empty_ledger(tmp_path):
    assert loopledger.consecutive_no_progress(str(tmp_path / "none.jsonl")) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_loopledger.py -k consecutive -v`
Expected: FAIL (`AttributeError: module 'sb.loopledger' has no attribute 'consecutive_no_progress'`)

- [ ] **Step 3: Implement**

In `sb/loopledger.py`, add after `diagnose`:
```python
def consecutive_no_progress(ledger_path):
    """Length of the trailing run of iterations with no task reaching `done`
    (spec §6). A done line resets the run. Idle waits aren't logged, so this is
    consecutive *task* iterations — the early-churn signal B's monitor flags."""
    run = 0
    for ln in _read(ledger_path):
        run = 0 if ln.get("outcome") == "done" else run + 1
    return run
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_loopledger.py -v`
Expected: PASS (all prior loopledger tests + 3 new)

- [ ] **Step 5: Commit**

```bash
git add sb/loopledger.py tests/test_loopledger.py
git commit -m "feat(sb): loopledger.consecutive_no_progress — early-churn signal (spec §6)"
```

---

### Task 3: Quota detector hook

**Files:**
- Create: `hooks/__init__.py` (empty — makes `hooks` importable)
- Create: `hooks/sb_quota.py`
- Test: `tests/test_quota.py`

Detection only (HDR-011): scan `tool_response.text` for rate-limit / 429 / usage-cap strings (token-free regex). On match, write `.switchboard/quota.json`. The worker reads it advisory; nothing gates a claim on it.

- [ ] **Step 1: Write the failing test**

`tests/test_quota.py`:
```python
import json
import os

from hooks import sb_quota


def test_detect_throttled_on_429(lay):
    state = sb_quota.detect("Error 429: rate limit exceeded, retry later")
    assert state["state"] == "throttled"


def test_detect_exhausted_on_usage_cap(lay):
    state = sb_quota.detect("You have reached your usage limit for this period")
    assert state["state"] == "exhausted"


def test_detect_clean_text_returns_none():
    assert sb_quota.detect("wrote 3 files, tests pass") is None
    assert sb_quota.detect("") is None
    assert sb_quota.detect(None) is None


def test_find_root_walks_up_from_worktree(lay, tmp_path):
    wt = os.path.join(lay.repo, ".worktrees", "w1")
    os.makedirs(wt)
    assert sb_quota.find_root(wt) == lay.repo


def test_find_root_returns_none_when_no_switchboard(tmp_path):
    assert sb_quota.find_root(str(tmp_path)) is None


def test_run_writes_quota_json_on_rate_limit(lay):
    payload = {"hook_event_name": "PostToolUse", "cwd": lay.repo,
               "tool_response": {"type": "text", "text": "HTTP 429 Too Many Requests"}}
    sb_quota.run(payload)
    q = json.load(open(os.path.join(lay.root, "quota.json")))
    assert q["state"] == "throttled"
    assert "at" in q


def test_run_noop_on_clean(lay):
    payload = {"hook_event_name": "PostToolUse", "cwd": lay.repo,
               "tool_response": {"type": "text", "text": "all good"}}
    sb_quota.run(payload)
    assert not os.path.exists(os.path.join(lay.root, "quota.json"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_quota.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'hooks'` until `__init__.py` + module exist)

- [ ] **Step 3: Implement**

Create `hooks/__init__.py` (empty file).

Create `hooks/sb_quota.py`:
```python
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


def find_root(cwd):
    """Walk up from cwd to the dir containing .switchboard/ (ADR-002). The
    subagent cwd is its worktree, which has no .switchboard. None if not found."""
    d = os.path.abspath(cwd or ".")
    while True:
        if os.path.isdir(os.path.join(d, ".switchboard")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_quota.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add hooks/__init__.py hooks/sb_quota.py tests/test_quota.py
git commit -m "feat(hooks): token-free quota detector -> quota.json (spec §4, HDR-011)"
```

---

### Task 4: Guard — rolling state + tripwire evaluation

**Files:**
- Create: `hooks/sb_guard.py` (state + tripwires this task; decisions + main in Task 5)
- Test: `tests/test_guard.py`

Pure functions: `new_state()`, `update_state(state, payload, cfg)` (rolling per-agent ledger), `evaluate(state, cfg) -> (tripped, signal, evidence)`. Mirrors `rabbit_guard.evaluate_tripwires` but keyed per-agent and progress-marker driven.

Progress marker (resets `no_progress`): a PostToolUse call counts as progress when `tool_name` is an edit tool (`Write`/`Edit`/`MultiEdit`/`NotebookEdit`) OR a `Bash` call whose command contains `git commit` OR a write to a `results/` path. Otherwise the no-progress counter increments.

- [ ] **Step 1: Write the failing test**

`tests/test_guard.py`:
```python
from hooks import sb_guard

CFG = {"guard_max_tool_calls": 80, "guard_max_wall_s": 1200,
       "guard_repeat_call": 3, "guard_repeat_error": 3, "guard_no_progress": 15,
       "guard_nudge_cap": 3, "guard_cooldown_calls": 3}


def post(tool_name, tool_input=None, text="", exit_code=0):
    return {"hook_event_name": "PostToolUse", "tool_name": tool_name,
            "tool_input": tool_input or {}, "agent_id": "a1",
            "tool_response": {"type": "text", "text": text}}


def feed(state, payload, cfg=CFG, n=1):
    for _ in range(n):
        state = sb_guard.update_state(state, payload, cfg)
    return state


def test_repeat_call_trips_at_threshold():
    st = sb_guard.new_state(now=0.0)
    st = feed(st, post("Bash", {"command": "ls"}), n=3)
    tripped, signal, _ = sb_guard.evaluate(st, CFG)
    assert tripped and signal == "repeat_call"


def test_distinct_calls_do_not_trip():
    st = sb_guard.new_state(now=0.0)
    for cmd in ("a", "b", "c", "d"):
        st = sb_guard.update_state(st, post("Bash", {"command": cmd}), CFG)
    assert not sb_guard.evaluate(st, CFG)[0]


def test_repeat_error_trips():
    st = sb_guard.new_state(now=0.0)
    st = feed(st, post("Bash", {"command": "x"},
                       text="Traceback: ValueError: boom"), n=3)
    tripped, signal, _ = sb_guard.evaluate(st, CFG)
    assert tripped and signal == "repeat_error"


def test_no_progress_trips_after_window():
    st = sb_guard.new_state(now=0.0)
    # 15 read-only calls (distinct, no edits/commits) -> no-progress
    for i in range(15):
        st = sb_guard.update_state(st, post("Read", {"file_path": f"/f{i}"}), CFG)
    tripped, signal, _ = sb_guard.evaluate(st, CFG)
    assert tripped and signal == "no_progress"


def test_edit_resets_no_progress():
    st = sb_guard.new_state(now=0.0)
    for i in range(14):
        st = sb_guard.update_state(st, post("Read", {"file_path": f"/f{i}"}), CFG)
    st = sb_guard.update_state(st, post("Write", {"file_path": "/x"}), CFG)
    assert st["since_progress"] == 0
    assert not sb_guard.evaluate(st, CFG)[0]


def test_git_commit_counts_as_progress():
    st = sb_guard.new_state(now=0.0)
    for i in range(14):
        st = sb_guard.update_state(st, post("Read", {"file_path": f"/f{i}"}), CFG)
    st = sb_guard.update_state(st, post("Bash", {"command": "git commit -m x"}), CFG)
    assert st["since_progress"] == 0


def test_tool_budget_trips():
    st = sb_guard.new_state(now=0.0)
    st["calls"] = 80
    tripped, signal, _ = sb_guard.evaluate(st, CFG)
    assert tripped and signal == "tool_budget"


def test_wallclock_budget_trips():
    st = sb_guard.new_state(now=0.0)
    st["calls"] = 1
    tripped, signal, _ = sb_guard.evaluate(st, CFG, now=1201.0)
    assert tripped and signal == "wallclock_budget"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_guard.py -v`
Expected: FAIL (`ModuleNotFoundError`/`AttributeError` — module not yet created)

- [ ] **Step 3: Implement**

Create `hooks/sb_guard.py` (state + tripwires; `main`/decisions added in Task 5):
```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_guard.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add hooks/sb_guard.py tests/test_guard.py
git commit -m "feat(hooks): guard rolling state + tripwire evaluation (spec §3)"
```

---

### Task 5: Guard — two-strike decisions + hook main()

**Files:**
- Modify: `hooks/sb_guard.py` (add `decide_post`, `decide_pre`, `load_state`/`save_state`, `main`)
- Test: `tests/test_guard.py` (append)

Two-strike: PostToolUse trip #1 → nudge (`additionalContext`), respecting cooldown + nudge-cap; trip #2 → arm `block_armed`. PreToolUse with `block_armed` → deny. State persisted per-agent under `.switchboard/guard/<agent_id>.json`. `agent_id` absent ⇒ no-op (the worker loop itself is never guarded).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_guard.py`:
```python
import json
import os


def test_decide_post_first_trip_nudges():
    st = sb_guard.new_state(now=0.0)
    st = feed(st, post("Bash", {"command": "ls"}), n=3)
    out, st = sb_guard.decide_post(st, CFG)
    assert out["hookSpecificOutput"]["additionalContext"]
    assert st["trips"] == 1 and st["block_armed"] is False


def test_decide_post_second_trip_arms_block():
    st = sb_guard.new_state(now=0.0)
    st = feed(st, post("Bash", {"command": "ls"}), n=3)
    _, st = sb_guard.decide_post(st, CFG)          # trip 1: nudge
    st["last_nudge_call"] = -999                    # clear cooldown for the test
    st = feed(st, post("Bash", {"command": "ls"}), n=1)
    _, st = sb_guard.decide_post(st, CFG)          # trip 2: arm
    assert st["block_armed"] is True


def test_decide_post_no_trip_is_silent():
    st = sb_guard.new_state(now=0.0)
    st = sb_guard.update_state(st, post("Read", {"file_path": "/a"}), CFG)
    out, st = sb_guard.decide_post(st, CFG)
    assert out is None and st["trips"] == 0


def test_decide_post_respects_nudge_cap():
    st = sb_guard.new_state(now=0.0)
    st["nudges"] = CFG["guard_nudge_cap"]          # cap already reached
    st = feed(st, post("Bash", {"command": "ls"}), n=3)
    out, st = sb_guard.decide_post(st, CFG)
    assert out is None                              # no more nudges
    assert st["block_armed"] is True                # but escalation still arms


def test_decide_pre_denies_when_armed():
    st = sb_guard.new_state(now=0.0)
    st["block_armed"] = True
    out = sb_guard.decide_pre(st)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "block" in out["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_decide_pre_allows_when_not_armed():
    assert sb_guard.decide_pre(sb_guard.new_state(now=0.0)) is None


def test_state_roundtrip(lay):
    st = sb_guard.new_state(now=5.0)
    st["calls"] = 7
    sb_guard.save_state(lay.repo, "agentX", st)
    assert sb_guard.load_state(lay.repo, "agentX", now=9.0)["calls"] == 7


def test_main_noop_without_agent_id(lay, capsys, monkeypatch):
    payload = {"hook_event_name": "PostToolUse", "cwd": lay.repo,
               "tool_name": "Bash", "tool_input": {"command": "ls"},
               "tool_response": {"type": "text", "text": ""}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    try:
        sb_guard.main()
    except SystemExit:
        pass
    assert capsys.readouterr().out == ""   # top-level session call: silent no-op
    assert not os.path.exists(os.path.join(lay.guard, "no_agent.json"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_guard.py -k "decide or state or main" -v`
Expected: FAIL (`AttributeError: ... has no attribute 'decide_post'`)

- [ ] **Step 3: Implement**

Append to `hooks/sb_guard.py`:
```python
def _nudge(signal, evidence):
    return {"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (f"[sb-guard:{signal}] {evidence}. You appear to be "
                              f"looping without progress — stop and reassess; do "
                              f"not repeat the same action.")}}


def decide_post(state, cfg, now=None):
    """Returns (output_dict_or_None, new_state). Trip #1 nudges (cooldown +
    cap respected); trip #2+ arms the PreToolUse block."""
    tripped, signal, evidence = evaluate(state, cfg, now=now)
    if not tripped:
        return None, state
    state["trips"] += 1
    if state["trips"] >= 2:
        state["block_armed"] = True
    can_nudge = (state["nudges"] < cfg["guard_nudge_cap"] and
                 state["calls"] - state["last_nudge_call"] >= cfg["guard_cooldown_calls"])
    if can_nudge:
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_guard.py -v`
Expected: PASS (all guard tests). Then run `.venv/bin/pytest -q` and expect all green.

- [ ] **Step 5: Commit**

```bash
git add hooks/sb_guard.py tests/test_guard.py
git commit -m "feat(hooks): guard two-strike nudge->deny + hook main (spec §3)"
```

---

### Task 6: External monitor (liveness + quota + early churn)

**Files:**
- Create: `hooks/sb_monitor.py`
- Test: `tests/test_monitor.py`

Token-free wrapper (spec §5, §6): runs `sb status --emit` + `sb notify` (existing edge-triggered notifications cover gates/pauses/quota/stale heartbeats), then checks each worker's loop-ledger for early churn (`consecutive_no_progress >= monitor_churn_threshold`) and fires a churn alert through the resolved channel — edge-triggered via its own small state file so it doesn't re-fire every run.

- [ ] **Step 1: Write the failing test**

`tests/test_monitor.py`:
```python
import json
import os

from hooks import sb_monitor
from sb import loopledger


def churning_ledger(lay, worker_id, n):
    led = os.path.join(lay.root, f"loop-ledger-{worker_id}.jsonl")
    for i in range(n):
        loopledger.append(led, i=i, claimed_id=f"P/PH/T-{i}", type="task",
                          outcome="released", released=True, wall_s=1.0)
    return led


def test_find_churning_workers(lay):
    churning_ledger(lay, "w1", 7)
    churning_ledger(lay, "w2", 2)
    out = dict(sb_monitor.find_churning_workers(lay, threshold=6))
    assert out == {"w1": 7}            # w2 below threshold excluded


def test_churn_alert_is_edge_triggered(lay):
    churning_ledger(lay, "w1", 7)
    sent = []
    ch = lambda title, body: sent.append((title, body))
    sb_monitor.check_churn(lay, threshold=6, channel=ch)
    sb_monitor.check_churn(lay, threshold=6, channel=ch)   # second run: no re-fire
    assert len(sent) == 1
    assert "w1" in sent[0][1]


def test_churn_refires_after_recovery(lay):
    led = churning_ledger(lay, "w1", 7)
    sent = []
    ch = lambda title, body: sent.append((title, body))
    sb_monitor.check_churn(lay, threshold=6, channel=ch)
    loopledger.append(led, i=99, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=1.0)  # progress
    sb_monitor.check_churn(lay, threshold=6, channel=ch)           # cleared
    for i in range(100, 107):
        loopledger.append(led, i=i, claimed_id=f"P/PH/T-{i}", type="task",
                          outcome="released", released=True, wall_s=1.0)
    sb_monitor.check_churn(lay, threshold=6, channel=ch)           # re-fires
    assert len(sent) == 2


def test_run_emits_digest_and_returns(lay):
    rc = sb_monitor.run(lay.repo, channel=lambda t, b: None)
    assert rc == 0
    assert os.path.exists(os.path.join(lay.root, "digest.json"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_monitor.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'hooks.sb_monitor'`)

- [ ] **Step 3: Implement**

Create `hooks/sb_monitor.py`:
```python
#!/usr/bin/env python3
"""Token-free external monitor (sub-plan B §5/§6). A scheduled job (launchd/cron)
that runs the existing `sb` read verbs (no model, no API key) so reporting keeps
working even when the whole fleet is capped or dead. Covers: quota state, stale
heartbeats (fleet stalled / silent session death), gates-ready, paused-for-human
(all via `sb status --emit` + `sb notify`), PLUS the early no-progress churn
detector that reads A's loop-ledger (the sharp signal A's coarse cap defers to).
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sb import channels, loopledger, paths, store
from sb.cli import main as sb_main

_CHURN_STATE = "monitor-churn-state.json"


def find_churning_workers(lay, threshold):
    """[(worker_id, run_length)] for ledgers whose trailing no-progress run is
    >= threshold (spec §6)."""
    out = []
    for path in sorted(glob.glob(os.path.join(lay.root, "loop-ledger-*.jsonl"))):
        base = os.path.basename(path)
        worker_id = base[len("loop-ledger-"):-len(".jsonl")]
        run = loopledger.consecutive_no_progress(path)
        if run >= threshold:
            out.append((worker_id, run))
    return out


def _churn_state_path(lay):
    return os.path.join(lay.root, _CHURN_STATE)


def check_churn(lay, threshold, channel):
    """Edge-triggered churn alert: fire only for workers newly at/over threshold;
    a worker that recovers drops out and can fire again later."""
    p = _churn_state_path(lay)
    seen = set(store.read_json(p).get("seen", [])) if os.path.exists(p) else set()
    churning = find_churning_workers(lay, threshold)
    live = {w for w, _ in churning}
    for worker_id, run in churning:
        if worker_id not in seen:
            channel("sb worker churning",
                    f"{worker_id}: {run} consecutive iterations with no task "
                    f"reaching done — investigate before the loop checkpoint")
    store.write_json(p, {"seen": sorted(live)})


def run(repo, channel=None):
    lay = paths.Layout(repo)
    cfg = paths.load_config(lay)
    sb_main(["status", "--emit", "--repo", repo])
    sb_main(["notify", "--repo", repo])
    ch = channel or channels.resolve(cfg.get("notify_channel", "macos"))
    check_churn(lay, cfg["monitor_churn_threshold"], ch)
    return 0


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "."
    try:
        return run(repo)
    except Exception as e:
        sys.stderr.write(f"sb_monitor error: {e}\n")
        return 0   # never let the scheduled job error-loop


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_monitor.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add hooks/sb_monitor.py tests/test_monitor.py
git commit -m "feat(hooks): token-free monitor — liveness/quota/notify + early churn (spec §5/§6)"
```

---

### Task 7: Wiring — settings example, launchd plist, delete v1 guard

**Files:**
- Modify: `hooks/settings.example.json`
- Create: `hooks/com.switchboard.monitor.plist.example`
- Delete: `rabbit_guard.py`

- [ ] **Step 1: Replace the hook wiring**

Overwrite `hooks/settings.example.json`:
```json
{
  "comment": "Merge into your .claude/settings.json. Token-free deterministic guards (no API key). sb_guard handles BOTH PreToolUse (2nd-strike deny) and PostToolUse (detection + 1st-strike nudge); sb_quota detects rate limits on PostToolUse. Use absolute paths.",
  "hooks": {
    "PreToolUse": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python3 /ABS/PATH/TO/hooks/sb_guard.py", "timeout": 10 }
      ] }
    ],
    "PostToolUse": [
      { "matcher": "*", "hooks": [
        { "type": "command", "command": "python3 /ABS/PATH/TO/hooks/sb_guard.py", "timeout": 10 },
        { "type": "command", "command": "python3 /ABS/PATH/TO/hooks/sb_quota.py", "timeout": 10 }
      ] }
    ]
  },
  "notes": [
    "No ANTHROPIC_API_KEY needed — the v1 paid reviewer is deleted; verification is A's lane.",
    "Guard state is per-subagent under .switchboard/guard/<agent_id>.json; top-level session calls are not guarded.",
    "Thresholds live in .switchboard/config.json (guard_* keys); all are tunable (spec §7)."
  ]
}
```

- [ ] **Step 2: Add the monitor scheduling example**

Create `hooks/com.switchboard.monitor.plist.example`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!-- launchd: run the token-free monitor every 5 minutes. Load with:
     launchctl load ~/Library/LaunchAgents/com.switchboard.monitor.plist
     Cron alternative (crontab -e):
       */5 * * * * /ABS/PATH/TO/.venv/bin/python /ABS/PATH/TO/hooks/sb_monitor.py /ABS/PATH/TO/REPO -->
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.switchboard.monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/ABS/PATH/TO/.venv/bin/python</string>
    <string>/ABS/PATH/TO/hooks/sb_monitor.py</string>
    <string>/ABS/PATH/TO/REPO</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/sb_monitor.err</string>
</dict>
</plist>
```

- [ ] **Step 3: Delete the v1 guard**

```bash
git rm rabbit_guard.py
```
(It is fully replaced: tripwires → `hooks/sb_guard.py`; the paid reviewer is deleted by design, spec §2/§6. Confirm no test imports it: `grep -rn rabbit_guard tests/` returns nothing.)

- [ ] **Step 4: Verify nothing references the deleted file**

Run: `grep -rn "rabbit_guard" . --include="*.py" --include="*.json" --include="*.md" | grep -v docs/`
Expected: no hits outside docs (docs may mention the v1 history).

- [ ] **Step 5: Commit**

```bash
git add hooks/settings.example.json hooks/com.switchboard.monitor.plist.example
git rm rabbit_guard.py
git commit -m "chore(hooks): wire sb_guard+sb_quota+monitor; delete v1 rabbit_guard"
```

---

### Task 8: Docs — mark B implemented

**Files:**
- Modify: `CLAUDE.md`, `docs/ROADMAP.md`

- [ ] **Step 1: Update the ROADMAP**

In `docs/ROADMAP.md`, change the B row in the milestone table to `**IMPLEMENTED** <date> — N tests` (use the real count from `.venv/bin/pytest -q`), and update the B bullet's status sentence to note it is built (guard, quota detector, monitor, churn detector; v1 guard deleted; ADR-001/002 recorded).

- [ ] **Step 2: Update CLAUDE.md State**

In `CLAUDE.md`, update the State block: B implemented (token-free guard/quota/monitor; rabbit_guard.py deleted — drop the "v1 leftover" caveat line since it no longer exists). Bump the test count. Add ADR-001/002 to the decisions reference if you keep an AgDR list.

- [ ] **Step 3: Final full-suite run**

Run: `.venv/bin/pytest -q`
Expected: PASS (all green).

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/ROADMAP.md
git commit -m "docs(sb): mark sub-plan B implemented (guards + quota + monitor)"
```

---

## Out of scope (deferred deliberately)

- **HDR-010 escalation routing** (interrupt/flag-async/record-silent tiers; independent tier judge) → sub-plan C. B provides the notify *firing* and deterministic guards only.
- **Per-task budget wiring** (task.budget → guard) → deferred past M0 (spec §3); M0 guard uses global `guard_*` config defaults.
- **SubagentStop guard-state cleanup** → not built (YAGNI; state files are tiny + transient). Add if `guard/` growth proves real.
- **Threshold tuning** → all `guard_*`/`monitor_*` defaults are first guesses; tune from real `loop-diagnostic` + guard-state data once A runs at volume (spec §7 revision condition).

## Self-review checklist (run after writing, before execution)

1. **Spec coverage:** §1 hook contract → "Hook contract" + Tasks 3/4/5; §2 components → Tasks 3 (quota), 4+5 (guard), 6 (monitor), 7 (wiring + delete reviewer); §3 tripwires + two-strike + fail-open → Tasks 4/5; §4 quota → Task 3; §5 monitor → Task 6; §6 early churn → Tasks 2 + 6; §7 budget=hooks + tunable thresholds → Task 1 (config) + "Out of scope"; §8 testing → every task is TDD; §9 deny→blocked resolved (A's `sb block` shipped) + not-C → "Out of scope". Covered.
2. **Placeholders:** none — every code/test step shows full content.
3. **Type/name consistency:** `find_root` defined in `sb_quota`, reused by the guard via `sb_quota_find_root`; `consecutive_no_progress` signature matches across loopledger + monitor; config keys identical across Task 1, guard tests (CFG), and `evaluate`; `agent_id` (not `agentId`) used throughout; `tool_response.text` (the verified field) read in both quota and guard.
