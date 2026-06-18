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
    tripped, signal, _ = sb_guard.evaluate(st, CFG, now=0.0)
    assert tripped and signal == "repeat_call"


def test_distinct_calls_do_not_trip():
    st = sb_guard.new_state(now=0.0)
    for cmd in ("a", "b", "c", "d"):
        st = sb_guard.update_state(st, post("Bash", {"command": cmd}), CFG)
    assert not sb_guard.evaluate(st, CFG, now=0.0)[0]


def test_repeat_error_trips():
    st = sb_guard.new_state(now=0.0)
    # distinct commands (so repeat_call does NOT trip) sharing one error sig
    for cmd in ("cat a", "cat b", "cat c"):
        st = sb_guard.update_state(
            st, post("Bash", {"command": cmd}, text="Traceback: ValueError: boom"), CFG)
    tripped, signal, _ = sb_guard.evaluate(st, CFG, now=0.0)
    assert tripped and signal == "repeat_error"


def test_no_progress_trips_after_window():
    st = sb_guard.new_state(now=0.0)
    # 15 read-only calls (distinct, no edits/commits) -> no-progress
    for i in range(15):
        st = sb_guard.update_state(st, post("Read", {"file_path": f"/f{i}"}), CFG)
    tripped, signal, _ = sb_guard.evaluate(st, CFG, now=0.0)
    assert tripped and signal == "no_progress"


def test_edit_resets_no_progress():
    st = sb_guard.new_state(now=0.0)
    for i in range(14):
        st = sb_guard.update_state(st, post("Read", {"file_path": f"/f{i}"}), CFG)
    st = sb_guard.update_state(st, post("Write", {"file_path": "/x"}), CFG)
    assert st["since_progress"] == 0
    assert not sb_guard.evaluate(st, CFG, now=0.0)[0]


def test_git_commit_counts_as_progress():
    st = sb_guard.new_state(now=0.0)
    for i in range(14):
        st = sb_guard.update_state(st, post("Read", {"file_path": f"/f{i}"}), CFG)
    st = sb_guard.update_state(st, post("Bash", {"command": "git commit -m x"}), CFG)
    assert st["since_progress"] == 0


def test_tool_budget_trips():
    st = sb_guard.new_state(now=0.0)
    st["calls"] = 80
    tripped, signal, _ = sb_guard.evaluate(st, CFG, now=0.0)
    assert tripped and signal == "tool_budget"


def test_wallclock_budget_trips():
    st = sb_guard.new_state(now=0.0)
    st["calls"] = 1
    tripped, signal, _ = sb_guard.evaluate(st, CFG, now=1201.0)
    assert tripped and signal == "wallclock_budget"
