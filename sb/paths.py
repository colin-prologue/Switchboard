"""Filesystem layout for a switchboard-enabled repo.

Transient orchestration state lives under .switchboard/ (gitignored).
Durable artifacts (decisions/, plans/) live at repo top level (tracked).
"""

import json
import os

LANES = ["queued", "active", "paused", "done", "failed"]

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


class Layout:
    def __init__(self, repo):
        self.repo = os.path.abspath(repo)
        self.root = os.path.join(self.repo, ".switchboard")
        self.tasks = os.path.join(self.root, "tasks")
        self.leases = os.path.join(self.root, "leases")
        self.heartbeats = os.path.join(self.root, "heartbeats")
        self.results = os.path.join(self.root, "results")
        self.guard = os.path.join(self.root, "guard")
        self.config_path = os.path.join(self.root, "config.json")
        self.decisions = os.path.join(self.repo, "decisions")
        self.plans = os.path.join(self.repo, "plans")

    def lane(self, name):
        if name not in LANES:
            raise ValueError(f"unknown lane {name!r}; valid: {LANES}")
        return os.path.join(self.tasks, name)


def init(repo):
    lay = Layout(repo)
    for lane in LANES:
        os.makedirs(lay.lane(lane), exist_ok=True)
    for d in [lay.leases, lay.heartbeats, lay.results]:
        os.makedirs(d, exist_ok=True)
    # durable; tracked in git, never wiped
    for d in [lay.decisions, lay.plans]:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(lay.config_path):
        with open(lay.config_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return lay


def load_config(lay):
    with open(lay.config_path, encoding="utf-8") as f:
        try:
            return {**DEFAULT_CONFIG, **json.load(f)}
        except json.JSONDecodeError as e:
            raise ValueError(f"corrupt config at {lay.config_path}: {e}") from e
