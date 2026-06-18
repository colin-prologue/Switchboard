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
