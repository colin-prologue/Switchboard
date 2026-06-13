"""The sb command. Machine-first: JSON on stdout, exit codes for control
flow (0 ok, 2 held/blocked, 3 nothing to claim). Skills consume this."""

import argparse
import json
import sys

from sb import claims, paths, query, results, seed, spawn, store


def _out(obj):
    print(json.dumps(obj, indent=2))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sb")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--repo", default=".")
        return p

    common(sub.add_parser("init"))

    p = common(sub.add_parser("claim"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--tier", default=None)
    p.add_argument("--wait", type=float, default=0)

    p = common(sub.add_parser("file-result"))
    p.add_argument("task_id")

    p = common(sub.add_parser("spawn"))
    p.add_argument("--task", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--tier", required=True)
    p.add_argument("--done", required=True)

    p = common(sub.add_parser("seed"))
    p.add_argument("--plan", required=True)
    p.add_argument("--repo-state", default="HEAD")
    p.add_argument("--force", action="store_true")

    common(sub.add_parser("requeue-stale"))

    p = common(sub.add_parser("query"))
    p.add_argument("--text", default=None)
    p.add_argument("--tags", default="")
    p.add_argument("--limit", type=int, default=8)

    p = common(sub.add_parser("heartbeat"))
    p.add_argument("--worker-id", required=True)

    a = ap.parse_args(argv)

    if a.cmd == "init":
        lay = paths.init(a.repo)
        _out({"initialized": lay.root})
        return 0

    lay = paths.Layout(a.repo)
    cfg = paths.load_config(lay)

    if a.cmd == "claim":
        task = claims.claim_wait(lay, a.worker_id, tier=a.tier, cfg=cfg,
                                 wait_s=a.wait)
        if task is None:
            return 3
        _out(task)
        return 0

    if a.cmd == "file-result":
        lane = results.file_result(lay, cfg, a.task_id)
        _out({"task_id": a.task_id, "lane": lane})
        return 0

    if a.cmd == "spawn":
        research = spawn.spawn_research(lay, cfg, a.task, goal=a.goal,
                                        tier=a.tier, done_statement=a.done)
        if research is None:
            _out({"spawned": None, "reason": "chain depth cap; parent paused"})
            return 2
        _out(research)
        return 0

    if a.cmd == "seed":
        plan = store.read_json(a.plan)
        try:
            seeded = seed.seed(lay, plan, repo_state=a.repo_state,
                               force=a.force)
        except (seed.BlockingQuestions, seed.AlreadySeeded) as e:
            print(json.dumps({"held": str(e)}), file=sys.stderr)
            return 2
        _out({"seeded": seeded})
        return 0

    if a.cmd == "requeue-stale":
        _out({"requeued": claims.requeue_stale(lay, cfg)})
        return 0

    if a.cmd == "query":
        tags = [t for t in a.tags.split(",") if t]
        _out(query.query(lay, tags=tags, text=a.text, limit=a.limit))
        return 0

    if a.cmd == "heartbeat":
        claims.heartbeat(lay, a.worker_id)
        _out({"heartbeat": a.worker_id})
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
