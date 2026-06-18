import json
import os

from sb import loopledger


def read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_append_writes_one_jsonl_line_per_call(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id="P/PH/T-1", type="task",
                      outcome="paused", released=False, wall_s=12.5)
    loopledger.append(led, i=1, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=8.0)
    lines = read_lines(led)
    assert len(lines) == 2
    assert lines[0] == {"i": 0, "claimed_id": "P/PH/T-1", "type": "task",
                        "outcome": "paused", "released": False, "wall_s": 12.5}
    assert lines[1]["outcome"] == "done"


def test_append_handles_idle_pass(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id=None, type="idle",
                      outcome="idle", released=False, wall_s=30.0)
    assert read_lines(led)[0]["claimed_id"] is None


def test_diagnose_classifies_productive_and_churn(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    # T-1 succeeds (paused→awaiting verify); its verify passes (done).
    loopledger.append(led, i=0, claimed_id="P/PH/T-1", type="task",
                      outcome="paused", released=False, wall_s=10.0)
    loopledger.append(led, i=1, claimed_id="P/PH/T-1.V1", type="verify",
                      outcome="done", released=False, wall_s=5.0)
    # T-2 hits a rate limit and is released (infra), then re-claimed and done.
    loopledger.append(led, i=2, claimed_id="P/PH/T-2", type="task",
                      outcome="released", released=True, wall_s=1.0)
    loopledger.append(led, i=3, claimed_id="P/PH/T-2", type="task",
                      outcome="paused", released=False, wall_s=9.0)
    loopledger.append(led, i=4, claimed_id="P/PH/T-2.V1", type="verify",
                      outcome="done", released=False, wall_s=4.0)
    # one idle pass
    loopledger.append(led, i=5, claimed_id=None, type="idle",
                      outcome="idle", released=False, wall_s=20.0)

    d = loopledger.diagnose(led, worker_id="w1")
    assert d["worker_id"] == "w1"
    assert d["total_iterations"] == 6
    assert d["distinct_tasks"] == 4          # T-1, T-1.V1, T-2, T-2.V1
    assert d["productive"] == 2              # two outcome==done lines
    assert d["releases"] == 1
    assert d["retries"] == 1                 # second P/PH/T-2 is a repeat claim
    assert d["churn"] == 2                   # releases + retries
    assert d["wall_s_total"] == 49.0


def test_diagnose_empty_ledger(tmp_path):
    led = str(tmp_path / "missing.jsonl")
    d = loopledger.diagnose(led, worker_id="w1")
    assert d["total_iterations"] == 0
    assert d["distinct_tasks"] == 0
    assert d["productive"] == 0
    assert d["churn"] == 0
    assert d["wall_s_total"] == 0


def test_diagnose_writes_out_file(tmp_path):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.append(led, i=0, claimed_id="P/PH/T-1", type="verify",
                      outcome="done", released=False, wall_s=3.0)
    out = str(tmp_path / "loop-diagnostic-w1.json")
    loopledger.diagnose(led, worker_id="w1", out=out)
    with open(out) as f:
        assert json.load(f)["productive"] == 1


def test_cli_append_then_diagnose(tmp_path, capsys):
    led = str(tmp_path / "loop-ledger-w1.jsonl")
    loopledger.main(["append", "--ledger", led, "--i", "0",
                     "--claimed-id", "P/PH/T-1.V1", "--type", "verify",
                     "--outcome", "done", "--wall-s", "3.0"])
    rc = loopledger.main(["diagnose", "--ledger", led, "--worker-id", "w1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["total_iterations"] == 1
    assert out["productive"] == 1
