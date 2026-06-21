import json
import os

import pytest

from sb import paths


def test_init_creates_layout(tmp_path):
    lay = paths.init(str(tmp_path))
    for lane in paths.LANES:
        assert os.path.isdir(lay.lane(lane))
    for d in [lay.leases, lay.heartbeats, lay.results, lay.decisions, lay.plans]:
        assert os.path.isdir(d)
    assert os.path.isfile(lay.config_path)


def test_init_is_idempotent_and_preserves_config(tmp_path):
    lay = paths.init(str(tmp_path))
    with open(lay.config_path, "w", encoding="utf-8") as f:
        json.dump({"max_attempts": 5}, f)
    paths.init(str(tmp_path))  # second init must not clobber
    cfg = paths.load_config(lay)
    assert cfg["max_attempts"] == 5


def test_load_config_merges_defaults(lay):
    cfg = paths.load_config(lay)
    assert cfg["verifier_tier"] == "sonnet"
    assert cfg["lease_ttl_s"] == 5400
    assert cfg["max_chain_depth"] == 3
    assert cfg["max_attempts"] == 3


def test_load_config_raises_on_corrupt_json(lay):
    with open(lay.config_path, "w", encoding="utf-8") as f:
        f.write("{not json")
    with pytest.raises(ValueError, match="corrupt config"):
        paths.load_config(lay)


def test_lane_rejects_unknown_name(lay):
    with pytest.raises(ValueError, match="unknown lane"):
        lay.lane("archived")


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
