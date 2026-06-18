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
