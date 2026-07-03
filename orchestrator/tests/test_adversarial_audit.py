"""Regression tests for the 2026-07-03 adversarial-audit fixes.

Covers the behavior changes from that audit:
- polling.interval_ms and agent.max_concurrent_agents reject non-positive
  values at validation time (previously: silent hot-loop / silent no-dispatch)
- agent.max_sessions_per_issue cannot be disabled (non-positive coerces to
  the default — parking is always on)
- an unexpected (non-TrackerError) failure in the retry timer reschedules
  instead of stranding the claim forever
- removing a required label mid-run releases the worker at reconciliation
  (core §11.1(3): required labels gate continuation, not just dispatch)
"""

from __future__ import annotations

import pytest

from orchestrator.types import WorkflowDefinition, WorkflowError
from orchestrator.workflow import Config, validate_dispatch

from test_integration import (  # shared harness (fake tracker/runner)
    _build_harness,
    make_issue,
    wait_for,
)


def _cfg(config: dict, tmp_path) -> Config:
    return Config(WorkflowDefinition(config=config, prompt_template="x"), tmp_path)


# --- config validation ----------------------------------------------------------

def test_polling_interval_nonpositive_is_rejected(tmp_path):
    for bad in (0, -1):
        cfg = _cfg({"polling": {"interval_ms": bad}}, tmp_path)
        with pytest.raises(WorkflowError):
            cfg.polling_interval_ms()


def test_validate_dispatch_covers_polling_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    cfg = _cfg({"tracker": {"kind": "github", "repo": "a/b"},
                "polling": {"interval_ms": 0}}, tmp_path)
    with pytest.raises(WorkflowError):
        validate_dispatch(cfg)


def test_max_concurrent_agents_nonpositive_is_rejected(tmp_path):
    cfg = _cfg({"agent": {"max_concurrent_agents": 0}}, tmp_path)
    with pytest.raises(WorkflowError):
        cfg.agent()


def test_session_cap_cannot_be_disabled(tmp_path):
    # <= 0 coerces to the default: parking (caps as diagnostic checkpoints)
    # is always on.
    cfg = _cfg({"agent": {"max_sessions_per_issue": 0}}, tmp_path)
    assert cfg.agent().max_sessions_per_issue == 3


# --- retry timer resilience -------------------------------------------------------

async def test_retry_timer_unexpected_error_keeps_claim_and_reschedules(
        tmp_path, monkeypatch):
    orch, tracker, _, _ = _build_harness(tmp_path, monkeypatch)

    async def boom():
        raise RuntimeError("payload-shape bug")

    tracker.fetch_candidate_issues = boom  # not a TrackerError
    orch._schedule_retry("node-1", "1", attempt=1, delay_ms=60_000)
    assert "node-1" in orch.claimed

    await orch._on_retry_timer("node-1")

    # The claim survives and a new retry is queued — the issue is NOT
    # stranded (pre-fix: the exception escaped and the claim leaked forever).
    assert "node-1" in orch.claimed
    assert "node-1" in orch.retry_attempts
    assert orch.retry_attempts["node-1"].attempt == 2
    orch._cancel_retry("node-1")


# --- required labels gate continuation ---------------------------------------------

REQUIRED_LABELS_TMPL = """---
tracker:
  kind: github
  repo: "acme/api"
  api_key: "test-token"
  required_labels: ["sb"]
  active_states: ["todo", "in progress"]
  terminal_states: ["closed"]
polling:
  interval_ms: 100
workspace:
  root: "{ws_root}"
agent:
  max_concurrent_agents: 2
  max_turns: 1
  max_retry_backoff_ms: 500
  max_sessions_per_issue: 2
claude:
  command: "unused-by-fake-runner"
  max_turns: 1
  turn_timeout_ms: 5000
  read_timeout_ms: 3000
  stall_timeout_ms: 0
---
Work {{{{ issue.identifier }}}}
"""


async def test_required_label_removed_midrun_releases_worker(tmp_path, monkeypatch):
    orch, tracker, runner, _ = _build_harness(
        tmp_path, monkeypatch, workflow_tmpl=REQUIRED_LABELS_TMPL)
    runner.hold = True

    labeled = make_issue(1)
    labeled.labels = ["status:todo", "sb"]
    tracker.candidates = [labeled]
    tracker.states = {"node-1": labeled}
    await orch._tick()
    await wait_for(lambda: "node-1" in orch.running)

    # Operator pulls the required label mid-run: still active state, but the
    # continuation gate must release the worker without retry or cleanup.
    unlabeled = make_issue(1)
    unlabeled.labels = ["status:todo"]
    tracker.states = {"node-1": unlabeled}
    tracker.candidates = []
    await orch._tick()

    assert "node-1" not in orch.running
    assert "node-1" not in orch.claimed
    assert "node-1" not in orch.retry_attempts
    runner.release.set()
