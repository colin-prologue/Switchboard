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
}


class Layout:
    def __init__(self, repo):
        self.repo = os.path.abspath(repo)
        self.root = os.path.join(self.repo, ".switchboard")
        self.tasks = os.path.join(self.root, "tasks")
        self.leases = os.path.join(self.root, "leases")
        self.heartbeats = os.path.join(self.root, "heartbeats")
        self.results = os.path.join(self.root, "results")
        self.config_path = os.path.join(self.root, "config.json")
        self.decisions = os.path.join(self.repo, "decisions")
        self.plans = os.path.join(self.repo, "plans")

    def lane(self, name):
        return os.path.join(self.tasks, name)


def init(repo):
    lay = Layout(repo)
    for lane in LANES:
        os.makedirs(lay.lane(lane), exist_ok=True)
    for d in [lay.leases, lay.heartbeats, lay.results, lay.decisions, lay.plans]:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(lay.config_path):
        with open(lay.config_path, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    return lay


def load_config(lay):
    with open(lay.config_path, encoding="utf-8") as f:
        return {**DEFAULT_CONFIG, **json.load(f)}
