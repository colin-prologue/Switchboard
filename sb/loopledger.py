"""Token-free loop instrumentation for the /sb-work worker loop (spec §8).

The skill shells out to this (`python -m sb.loopledger ...`) once per iteration
to append to a per-worker JSONL ledger, and once at the loop-cap checkpoint to
compute the productive-vs-churn diagnostic. Pure functions over disk state — no
model reasoning, so the bookkeeping costs no tokens and survives session death.

Deliberately off the engine import graph: this is skill-support instrumentation,
not part of the deterministic git-free engine core.
"""

import argparse
import json
import os
import sys


def append(ledger_path, *, i, claimed_id, type, outcome, released, wall_s):
    os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)
    line = {"i": i, "claimed_id": claimed_id, "type": type,
            "outcome": outcome, "released": bool(released), "wall_s": wall_s}
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def _read(ledger_path):
    if not os.path.exists(ledger_path):
        return []
    out = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                # A partial write (e.g. session killed mid-line) raises here —
                # intentional: a corrupt ledger should surface loudly, never
                # produce a silently undercounted diagnostic.
                out.append(json.loads(line))
    return out


def diagnose(ledger_path, *, worker_id, out=None):
    lines = _read(ledger_path)
    seen = set()
    distinct = set()
    productive = retries = releases = 0
    wall_total = 0.0
    for ln in lines:
        cid = ln.get("claimed_id")
        if cid is not None:
            if cid in seen:
                retries += 1
            seen.add(cid)
            distinct.add(cid)
        # productive == distinct tasks reaching done, given the loop writes
        # exactly one done-outcome line per completed task (one verify pass).
        if ln.get("outcome") == "done":
            productive += 1
        if ln.get("released"):
            releases += 1
        wall_total += ln.get("wall_s", 0) or 0
    diag = {
        "worker_id": worker_id,
        "total_iterations": len(lines),
        "distinct_tasks": len(distinct),
        "productive": productive,
        "retries": retries,
        "releases": releases,
        "churn": releases + retries,
        "wall_s_total": wall_total,
    }
    if out is not None:
        tmp = f"{out}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(diag, f, indent=2)
        os.replace(tmp, out)
    return diag


def consecutive_no_progress(ledger_path):
    """Length of the trailing run of iterations with no task reaching `done`
    (spec §6). A done line resets the run. Idle waits aren't logged, so this is
    consecutive *task* iterations — the early-churn signal B's monitor flags."""
    run = 0
    for ln in _read(ledger_path):
        run = 0 if ln.get("outcome") == "done" else run + 1
    return run


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sb.loopledger")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("append")
    a.add_argument("--ledger", required=True)
    a.add_argument("--i", type=int, required=True)
    a.add_argument("--claimed-id", default=None)
    a.add_argument("--type", required=True)
    a.add_argument("--outcome", required=True)
    a.add_argument("--released", action="store_true")
    a.add_argument("--wall-s", type=float, default=0.0)

    d = sub.add_parser("diagnose")
    d.add_argument("--ledger", required=True)
    d.add_argument("--worker-id", required=True)
    d.add_argument("--out", default=None)

    ns = ap.parse_args(argv)
    if ns.cmd == "append":
        append(ns.ledger, i=ns.i, claimed_id=ns.claimed_id, type=ns.type,
               outcome=ns.outcome, released=ns.released, wall_s=ns.wall_s)
        return 0
    if ns.cmd == "diagnose":
        diag = diagnose(ns.ledger, worker_id=ns.worker_id, out=ns.out)
        print(json.dumps(diag, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
