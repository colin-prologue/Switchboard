#!/usr/bin/env python3
"""worker.py — a tier-pinned worker for the git-backed file queue.

One long-running supervisor process, pinned to a model tier (opus/sonnet/haiku/fable).
It loops: pull the queue, claim one matching task whose dependencies are met, run it in
a FRESH model session, validate the result, write it back, commit, repeat. The supervisor
is the long-lived thing; each TASK gets a brand-new model session, so context never bleeds
from one task into the next — that is the "clear context, one task at a time" hygiene, by
construction rather than by manual compaction.

Queue layout (all git-tracked, so a local terminal and a cloud session coordinate through
the same repo):

    .tasks/queued/    <- claimable
    .tasks/active/    <- claimed, running
    .tasks/paused/    <- halted for research or human
    .tasks/done/      <- finished, result written
    .tasks/failed/    <- gave up; reason written

Claiming is a move between lanes + commit + push. Git is the coordination layer: if two
workers grab the same task, the loser's push is rejected, it rebases, sees the task already
claimed, and picks another. Fine for modest pools; for heavy parallelism swap in a real
lock service — the rest of the design is unchanged.

Design rules: FAIL OPEN (a crash in one task never kills the loop or the queue), and the
worker never parses freeform model output — the model session writes a result file against
task.schema.json#/result, and the worker only validates and files it.
"""

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time

LANES = ["queued", "active", "paused", "done", "failed"]
TERMINAL = {"done", "failed"}


# ----------------------------------------------------------------- helpers
def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def log(msg):
    sys.stderr.write(f"[{now()}] {msg}\n")
    sys.stderr.flush()


def git(repo, *args, check=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def lane_dir(repo, lane):
    return os.path.join(repo, ".tasks", lane)


def ensure_layout(repo):
    for lane in LANES:
        os.makedirs(lane_dir(repo, lane), exist_ok=True)
    os.makedirs(os.path.join(repo, ".decisions"), exist_ok=True)
    os.makedirs(os.path.join(repo, ".results"), exist_ok=True)


def read_task(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_task(path, task):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(task, f, indent=2)
    os.replace(tmp, path)  # atomic on local fs


def fname(task):
    return task["id"].replace("/", "_") + ".json"


def done_ids(repo):
    return {read_task(os.path.join(lane_dir(repo, "done"), f))["id"]
            for f in os.listdir(lane_dir(repo, "done")) if f.endswith(".json")}


def deps_met(task, completed):
    return all(d in completed for d in task.get("context", {}).get("depends_on", []))


# ------------------------------------------------------------- claim / file
def find_claimable(repo, tier):
    completed = done_ids(repo)
    cands = []
    qdir = lane_dir(repo, "queued")
    for f in sorted(os.listdir(qdir)):
        if not f.endswith(".json"):
            continue
        try:
            t = read_task(os.path.join(qdir, f))
        except Exception:
            continue
        if t.get("tier") == tier and deps_met(t, completed):
            cands.append((f, t))
    return cands  # oldest-first by filename sort


def claim(repo, worker_id, f, task):
    """Move queued -> active, stamp the claim, commit, push. Returns True if we own it."""
    src = os.path.join(lane_dir(repo, "queued"), f)
    dst = os.path.join(lane_dir(repo, "active"), f)
    if not os.path.exists(src):
        return False  # someone else took it between listing and claiming
    task["status"] = "claimed"
    task["claim"] = {"worker_id": worker_id, "claimed_at": now()}
    task["attempts"] = task.get("attempts", 0) + 1
    shutil.move(src, dst)
    write_task(dst, task)
    git(repo, "add", "-A", ".tasks")
    git(repo, "commit", "-m", f"claim {task['id']} by {worker_id}")
    ok, _ = git(repo, "push")
    if not ok:
        # someone else pushed first — rebase and check whether the task is still ours
        git(repo, "pull", "--rebase")
        if not os.path.exists(dst):  # our claim lost the race
            return False
        git(repo, "push")
    return True


def file_result(repo, task, lane):
    """Move active -> {lane}, persist task, commit, push."""
    cur = os.path.join(lane_dir(repo, "active"), fname(task))
    dst = os.path.join(lane_dir(repo, lane), fname(task))
    if os.path.exists(cur):
        shutil.move(cur, dst)
    write_task(dst, task)
    git(repo, "add", "-A", ".tasks", ".decisions", ".results")
    git(repo, "commit", "-m", f"{lane} {task['id']}")
    if not git(repo, "push")[0]:
        git(repo, "pull", "--rebase")
        git(repo, "push")


# -------------------------------------------------------------- execution
def build_prompt(repo, task):
    """The instruction handed to a fresh model session. The session does the work and
    WRITES its result to .results/<id>.json (matching task.schema.json#/result) plus any
    decision files to .decisions/ with provenance = task.source. The worker reads that back."""
    rid = task["id"].replace("/", "_")
    return (
        f"You are a {task['tier']}-tier worker. Repo: {repo}\n"
        f"Check out {task.get('context', {}).get('repo_state', 'HEAD')} first.\n\n"
        f"GOAL: {task['goal']}\n"
        f"DONE WHEN: {task['done']['statement']}\n"
        f"VERIFY: {json.dumps(task['done'].get('verify', {}))}\n"
        f"CONSTRAINTS: {task.get('context', {}).get('constraints', [])}\n"
        f"GROUNDING (read these decisions first): {task.get('context', {}).get('grounding', [])}\n"
        f"DECISION POINTS: {json.dumps(task.get('decision_points', []))}\n"
        f"  - For any point with halt=true, pause, research, and write an ADR before proceeding.\n\n"
        f"When finished, write your result to .results/{rid}.json matching the result schema "
        f"(outcome, summary, evidence, decisions_emitted, unblocks), and write any decision "
        f"records to .decisions/ with provenance {json.dumps(task.get('source', {}))}."
    )


def execute(repo, task, executor_cmd):
    """Spawn a FRESH model session for this task. Returns a result dict.

    executor_cmd is a shell template with {prompt_file} and {tier}. In production this is your
    Claude Code / SDK invocation pinned to the tier's model. The model writes .results/<id>.json;
    we read it back. If unset, we no-op safely so the loop is runnable as a dry run."""
    rid = task["id"].replace("/", "_")
    result_path = os.path.join(repo, ".results", f"{rid}.json")
    if os.path.exists(result_path):
        os.remove(result_path)

    if not executor_cmd:
        return {"outcome": "blocked", "summary": "No executor wired (dry run). Set --executor.",
                "completed_at": now()}

    prompt_path = os.path.join(repo, ".results", f"{rid}.prompt.txt")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(build_prompt(repo, task))
    cmd = executor_cmd.format(prompt_file=prompt_path, tier=task["tier"])
    budget = task.get("budget", {}).get("wallclock_s", 1800)
    try:
        subprocess.run(cmd, shell=True, cwd=repo, timeout=budget)
    except subprocess.TimeoutExpired:
        return {"outcome": "blocked", "summary": f"Wallclock budget {budget}s exceeded.", "completed_at": now()}

    if not os.path.exists(result_path):
        return {"outcome": "blocked", "summary": "Session ended without writing a result file.", "completed_at": now()}
    try:
        res = read_task(result_path)
    except Exception as e:
        return {"outcome": "blocked", "summary": f"Result file unparseable: {e}", "completed_at": now()}
    res.setdefault("completed_at", now())
    return res


def verify_ok(repo, task):
    """Run the machine exit condition, if it is a command. Other kinds are advisory here."""
    v = task.get("done", {}).get("verify", {})
    if v.get("kind") == "command":
        return git and subprocess.run(v["ref"], shell=True, cwd=repo).returncode == 0
    return True  # test/review/metric verification is the model session's responsibility


# ------------------------------------------------------------------- loop
def process_one(repo, tier, worker_id, executor_cmd, max_attempts):
    git(repo, "pull", "--rebase")
    for f, task in find_claimable(repo, tier):
        if task.get("attempts", 0) >= max_attempts:
            task["status"] = "failed"
            task["failure"] = {"reason": f"exceeded {max_attempts} attempts; escalate to human"}
            shutil.move(os.path.join(lane_dir(repo, "queued"), f), os.path.join(lane_dir(repo, "failed"), f))
            file_result(repo, task, "failed")
            return True
        if not claim(repo, worker_id, f, task):
            continue  # lost the race; try the next candidate
        log(f"claimed {task['id']}")
        task["status"] = "in_progress"
        write_task(os.path.join(lane_dir(repo, "active"), fname(task)), task)

        result = execute(repo, task, executor_cmd)
        task["result"] = result
        outcome = result.get("outcome")

        if outcome == "success" and verify_ok(repo, task):
            task["status"] = "done"
            file_result(repo, task, "done")
            log(f"done {task['id']}")
        elif outcome == "blocked":
            task["status"] = "paused_for_human"
            file_result(repo, task, "paused")
            log(f"paused {task['id']}: {result.get('summary','')}")
        else:  # partial or failed verification -> back to queue for another attempt
            task["status"] = "queued"
            task.pop("claim", None)
            shutil.move(os.path.join(lane_dir(repo, "active"), fname(task)),
                        os.path.join(lane_dir(repo, "queued"), fname(task)))
            file_result(repo, task, "queued")
            log(f"requeued {task['id']} (outcome={outcome})")
        return True
    return False  # nothing claimable right now


def main():
    ap = argparse.ArgumentParser(description="Tier-pinned worker for the git-backed file queue.")
    ap.add_argument("--tier", required=True, choices=["fable", "opus", "sonnet", "haiku"])
    ap.add_argument("--repo", default=".", help="Path to the git repo holding .tasks/")
    ap.add_argument("--worker-id", default=f"{os.uname().nodename}-{os.getpid()}")
    ap.add_argument("--executor", default="", help="Shell template for the model session, with {prompt_file} and {tier}. Empty = dry run.")
    ap.add_argument("--poll", type=int, default=15, help="Seconds to sleep when the queue is empty.")
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("--once", action="store_true", help="Process a single task and exit (for testing).")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo)
    ensure_layout(repo)
    log(f"worker {args.worker_id} up on tier={args.tier} repo={repo}")

    while True:
        try:
            did = process_one(repo, args.tier, args.worker_id, args.executor, args.max_attempts)
        except Exception as e:
            log(f"loop error (continuing): {type(e).__name__}: {e}")
            did = False
        if args.once:
            break
        if not did:
            time.sleep(args.poll)


if __name__ == "__main__":
    main()
