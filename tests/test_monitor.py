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
