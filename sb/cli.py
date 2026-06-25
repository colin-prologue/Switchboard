"""The sb command. Machine-first: JSON on stdout, exit codes for control
flow (0 ok, 2 held/blocked, 3 nothing to claim). Skills consume this."""

import argparse
import json
import os
import sys

from sb import (brief as brief_mod, channels, claims, digest as digest_mod,
                notify as notify_mod, paths, query, resolve, results, seed,
                spawn, stamp as stamp_mod, store)


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

    p = common(sub.add_parser("result"))
    p.add_argument("task_id")

    p = common(sub.add_parser("spawn"))
    p.add_argument("--task", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--tier", required=True)
    p.add_argument("--done", required=True)

    p = common(sub.add_parser("seed"))
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--plan", help="path to a plan json to expand into tasks")
    g.add_argument("--goal", help="raw goal; enqueues one planner task")
    g.add_argument("--goal-file", dest="goal_file",
                   help="read the goal from a file ('-' = stdin); enqueues "
                        "one planner task")
    p.add_argument("--repo-state", default="HEAD")
    p.add_argument("--tier", default="opus",
                   help="planner tier for --goal/--goal-file (default opus)")
    p.add_argument("--force", action="store_true")

    common(sub.add_parser("requeue-stale"))

    p = common(sub.add_parser("release"))
    p.add_argument("task_id")

    p = common(sub.add_parser("block"))
    p.add_argument("task_id")
    p.add_argument("--reason", default="subagent returned no result file")

    p = common(sub.add_parser("resolve"))
    p.add_argument("task_id")
    p.add_argument("--cause", default=None)
    p.add_argument("--fix", default=None)
    p.add_argument("--rule", default=None)

    p = common(sub.add_parser("query"))
    p.add_argument("--text", default=None)
    p.add_argument("--tags", default="")
    p.add_argument("--limit", type=int, default=8)

    p = common(sub.add_parser("heartbeat"))
    p.add_argument("--worker-id", required=True)

    p = common(sub.add_parser("brief"))
    p.add_argument("--plan", required=True, help="plan id, e.g. PLAN-001")
    p.add_argument("--phase", required=True)
    p.add_argument("--write", action="store_true",
                   help="also write reviews/<plan>_<phase>.md")

    p = common(sub.add_parser("stamp"))
    p.add_argument("--plan", required=True, help="plan id, e.g. PLAN-001")
    p.add_argument("--phase", required=True)
    p.add_argument("--action", required=True,
                   choices=["approve", "revise", "flag"])
    p.add_argument("--note", default="")
    p.add_argument("--reviewer", default="colin")
    p.add_argument("--decision", dest="target", default=None,
                   help="target one decision id; default = all phase AgDRs")
    p.add_argument("--force", action="store_true",
                   help="approve even if work tasks are unfinished")

    p = common(sub.add_parser("status"))
    p.add_argument("--emit", action="store_true",
                   help="persist the digest to .switchboard/digest.json")

    p = common(sub.add_parser("notify"))
    p.add_argument("--channel", default=None,
                   help="override notify_channel: macos|stdout|null")

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

    if a.cmd == "result":
        res = results.read_result(lay, a.task_id)
        if res is None:
            print(json.dumps({"task_id": a.task_id, "result": None}))
            return 3
        _out(res)
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
        goal = a.goal
        if a.goal_file:
            if a.goal_file == "-":
                goal = sys.stdin.read().strip()
            else:
                with open(a.goal_file, encoding="utf-8") as f:
                    goal = f.read().strip()
        if goal is not None:
            if not goal.strip():
                print(json.dumps({"held": "empty goal"}), file=sys.stderr)
                return 2
            cid = seed.seed_goal(lay, goal, repo_state=a.repo_state,
                                 tier=a.tier)
            _out({"seeded": [cid]})
            return 0
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

    if a.cmd == "release":
        dest = claims.release(lay, a.task_id)
        _out({"task_id": a.task_id, "lane": dest})
        return 0

    if a.cmd == "block":
        dest = results.block(lay, cfg, a.task_id, a.reason)
        _out({"task_id": a.task_id, "lane": dest})
        return 0

    if a.cmd == "resolve":
        rec_id = resolve.resolve(lay, cfg, a.task_id,
                                 cause=a.cause, fix=a.fix, rule=a.rule)
        _out({"task_id": a.task_id, "lane": "queued", "record": rec_id})
        return 0

    if a.cmd == "query":
        tags = [t for t in a.tags.split(",") if t]
        _out(query.query(lay, tags=tags, text=a.text, limit=a.limit))
        return 0

    if a.cmd == "heartbeat":
        claims.heartbeat(lay, a.worker_id)
        _out({"heartbeat": a.worker_id})
        return 0

    if a.cmd == "status":
        dg = digest_mod.build_digest(lay, cfg)
        if a.emit:
            store.write_json(os.path.join(lay.root, "digest.json"), dg)
        _out(dg)
        return 0

    if a.cmd == "brief":
        plan = store.read_json(os.path.join(lay.plans, f"{a.plan}.json"))
        md = brief_mod.build_brief(lay, plan, a.phase)
        if a.write:
            os.makedirs(os.path.join(lay.repo, "reviews"), exist_ok=True)
            with open(os.path.join(lay.repo, "reviews",
                                   f"{a.plan}_{a.phase}.md"),
                      "w", encoding="utf-8") as f:
                f.write(md)
        print(md)
        return 0

    if a.cmd == "stamp":
        try:
            out = stamp_mod.stamp(lay, a.plan, a.phase, action=a.action,
                                  note=a.note, reviewer=a.reviewer,
                                  target=a.target, force=a.force)
        except stamp_mod.GateNotReady as e:
            print(json.dumps({"held": str(e)}), file=sys.stderr)
            return 2
        _out(out)
        return 0

    if a.cmd == "notify":
        ch = channels.resolve(a.channel) if a.channel else None
        events = notify_mod.notify(lay, cfg, channel=ch)
        _out({"fired": [e["key"] for e in events]})
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
