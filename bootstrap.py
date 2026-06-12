#!/usr/bin/env python3
"""bootstrap.py — the front door: goal in, planned + queued work out.

What it does, in one command from a fresh terminal:
  1. take a GOAL,
  2. pull relevant PRECEDENT from the decision log (deterministic, zero-token),
  3. run the PLANNER in a fresh session to emit a plan + a decomposition decision,
  4. validate, and if any open question is BLOCKING, stop for human sign-off,
  5. SEED the git-backed queue with one task file per plan task, then commit.

It follows the worker's discipline: it never parses freeform model output. The planner
session WRITES a plan file (against plan.schema.json) and a decomposition decision
(against decision-record.schema.json); the harness reads those back, validates, and seeds.

Two modes:
  full:   bootstrap.py --goal "..." --executor '<model session cmd>'
  seed:   bootstrap.py --plan plans/PLAN-031.json     # skip planning, seed a ready plan
          (use the seed mode to test the queue path today, against the example plan)
"""

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys

import query_decisions  # reused for grounding


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def die(msg, code=1):
    sys.stderr.write(f"error: {msg}\n")
    sys.exit(code)


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def ensure_layout(repo):
    for lane in ["queued", "active", "paused", "done", "failed"]:
        os.makedirs(os.path.join(repo, ".tasks", lane), exist_ok=True)
    for d in [".decisions", ".results", "plans"]:
        os.makedirs(os.path.join(repo, d), exist_ok=True)


def head(repo):
    p = git(repo, "rev-parse", "HEAD")
    return p.stdout.strip() if p.returncode == 0 else "HEAD"


def load_tiers(repo, here):
    for cand in [os.path.join(repo, "tiers.json"), os.path.join(here, "tiers.json")]:
        if os.path.exists(cand):
            with open(cand, encoding="utf-8") as f:
                return json.load(f)["tiers"]
    return {"fable": "claude-fable-5", "opus": "claude-opus-4-8",
            "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5"}


def next_id(dirpath, prefix, pad=3):
    hi = 0
    if os.path.isdir(dirpath):
        for f in os.listdir(dirpath):
            m = re.search(rf"{prefix}-(\d+)", f)
            if m:
                hi = max(hi, int(m.group(1)))
    return f"{prefix}-{hi + 1:0{pad}d}"


# --------------------------------------------------------------- planning
def build_planner_prompt(goal, constraints, precedent, plan_id, sdr_id, repo, here):
    return (
        "You are the orchestrator. Decompose the goal into a plan with tiered model routing.\n"
        f"Write the plan to plans/{plan_id}.json against the schema at {here}/schemas/plan.schema.json, "
        f"with plan_id={plan_id} and decision_ref={sdr_id}.\n"
        f"Also write the decomposition decision to .decisions/{sdr_id}.json against "
        f"{here}/schemas/decision-record.schema.json (type=synthesis, id={sdr_id}), recording WHY you "
        "split the work this way and chose each tier.\n\n"
        f"GOAL: {goal}\n"
        f"CONSTRAINTS: {json.dumps(constraints)}\n"
        f"RELEVANT PRECEDENT (ground your choices in these; cite their ids in `grounding`):\n"
        f"{json.dumps(precedent, indent=2)}\n\n"
        "Routing guidance: reserve the top tier for the genuinely hard, compounding decisions; "
        "push mechanical work to the cheapest tier that clears the bar. Put meaningful human "
        "oversight at phase GATES, not inside phases. Flag any genuine ambiguity as a blocking "
        "open_question rather than guessing."
    )


def run_planner(repo, here, prompt, planner_model, executor):
    if not executor:
        return False  # dry run
    ppath = os.path.join(repo, ".results", "planner.prompt.txt")
    with open(ppath, "w", encoding="utf-8") as f:
        f.write(prompt)
    cmd = executor.format(prompt_file=ppath, model=planner_model, tier="planner")
    subprocess.run(cmd, shell=True, cwd=repo)
    return True


def validate_plan(plan):
    if not plan.get("plan_id") or not plan.get("phases"):
        die("plan missing plan_id or phases")
    for ph in plan["phases"]:
        for t in ph.get("tasks", []):
            if "done" not in t or "statement" not in t["done"]:
                die(f"task {t.get('task_id','?')} missing a done.statement")


# ----------------------------------------------------------------- seeding
def composite(plan_id, phase_id, task_id):
    return f"{plan_id}/{phase_id}/{task_id}"


def seed_queue(repo, plan, precedent, planner_model):
    plan_id = plan["plan_id"]
    # map task_id -> phase_id so depends_on (which are task-local) resolve to composite ids
    where = {t["task_id"]: ph["phase_id"] for ph in plan["phases"] for t in ph.get("tasks", [])}
    plan_grounding = plan.get("grounding", [])
    seeded = []
    repo_state = head(repo)

    prev_gate = None  # composite id of the previous phase's human gate, if any
    for ph in plan["phases"]:
        phase_cids = []
        for t in ph.get("tasks", []):
            cid = composite(plan_id, ph["phase_id"], t["task_id"])
            tier = t.get("model") or ph["default_model"]
            ttags = t.get("tags", [])
            # per-task grounding: plan-level + precedent whose tags intersect this task
            ground = list(dict.fromkeys(
                plan_grounding + [p["id"] for p in precedent if set(p.get("tags", [])) & set(ttags)]
            ))
            deps = [composite(plan_id, where[d], d) for d in t.get("depends_on", []) if d in where]
            if prev_gate:
                deps.append(prev_gate)  # block this phase until the prior human gate is approved

            dps = []
            de = t.get("decision_expected")
            if de:
                dps.append({
                    "label": de.get("about", t["task_id"]),
                    "options": [],
                    "halt": not de.get("auto_approve_if"),
                    "grounding": ground,
                })

            task = {
                "schema_version": "0.1.0",
                "id": cid,
                "tier": tier,
                "status": "queued",
                "source": {"plan_id": plan_id, "phase_id": ph["phase_id"], "task_id": t["task_id"]},
                "goal": t["title"],
                "context": {
                    "repo_state": repo_state,
                    "grounding": ground,
                    "constraints": plan.get("constraints", []),
                    "depends_on": deps,
                },
                "done": t["done"],
                "decision_points": dps,
                "attempts": 0,
                "created_at": now(),
                "created_by": planner_model,
            }
            if t.get("budget") or ph.get("budget"):
                task["budget"] = t.get("budget") or ph["budget"]

            path = os.path.join(repo, ".tasks", "queued", cid.replace("/", "_") + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(task, f, indent=2)
            seeded.append((cid, tier, bool(deps)))
            phase_cids.append(cid)

        # human gate: seed a GATE placeholder (in the paused lane, so no worker runs it) that
        # the next phase depends on. Only `gate.py stamp --approve` moves it to done.
        if ph.get("gate", {}).get("type") == "human":
            gate_cid = composite(plan_id, ph["phase_id"], "GATE")
            gate_task = {
                "schema_version": "0.1.0",
                "id": gate_cid,
                "tier": "fable",
                "status": "paused_for_human",
                "source": {"plan_id": plan_id, "phase_id": ph["phase_id"], "task_id": "GATE"},
                "goal": f"Human review gate: {ph['name']}",
                "context": {"repo_state": repo_state, "depends_on": phase_cids},
                "done": {"statement": ph.get("gate", {}).get("condition", "reviewer approves the phase")},
                "attempts": 0,
                "created_at": now(),
                "created_by": planner_model,
            }
            gpath = os.path.join(repo, ".tasks", "paused", gate_cid.replace("/", "_") + ".json")
            with open(gpath, "w", encoding="utf-8") as f:
                json.dump(gate_task, f, indent=2)
            prev_gate = gate_cid
        else:
            prev_gate = None
    return seeded


def main():
    ap = argparse.ArgumentParser(description="Front door: goal -> plan -> seeded queue.")
    ap.add_argument("--goal", help="the goal to plan and orchestrate")
    ap.add_argument("--plan", help="seed from a ready plan file, skipping planning")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--planner-tier", default="fable", choices=["fable", "opus", "sonnet", "haiku"])
    ap.add_argument("--constraints", default="", help="comma-separated hard constraints")
    ap.add_argument("--executor", default="", help="shell template for the planner session, with {prompt_file} {model}. Empty = dry run.")
    ap.add_argument("--force", action="store_true", help="seed even if blocking questions remain")
    a = ap.parse_args()

    repo = os.path.abspath(a.repo)
    here = os.path.dirname(os.path.abspath(__file__))
    ensure_layout(repo)
    tiers = load_tiers(repo, here)
    precedent = []

    # ----- get a plan: either provided, or generate one -----
    if a.plan:
        with open(a.plan, encoding="utf-8") as f:
            plan = json.load(f)
        planner_model = plan.get("author", {}).get("id", "unknown")
    else:
        if not a.goal:
            die("provide --goal (to plan) or --plan (to seed a ready plan)")
        constraints = [c.strip() for c in a.constraints.split(",") if c.strip()]
        precedent = query_decisions.query(repo, text=a.goal, limit=8)
        plan_id = next_id(os.path.join(repo, "plans"), "PLAN")
        sdr_id = next_id(os.path.join(repo, ".decisions"), "SDR")
        planner_model = tiers[a.planner_tier]
        prompt = build_planner_prompt(a.goal, constraints, precedent, plan_id, sdr_id, repo, here)

        print(f"planner: {planner_model}   precedent found: {len(precedent)}")
        if not run_planner(repo, here, prompt, planner_model, a.executor):
            print("\n[dry run] no --executor wired. The planner prompt is written to "
                  ".results/planner.prompt.txt.\nTo test the seeding path now, run with "
                  "--plan plans/PLAN-031.json against the example plan.")
            return
        plan_path = os.path.join(repo, "plans", f"{plan_id}.json")
        if not os.path.exists(plan_path):
            die("planner session ended without writing a plan file")
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)

    validate_plan(plan)

    # ----- pre-execution human gate: blocking open questions stop the loop -----
    blocking = [q for q in plan.get("open_questions", []) if q.get("blocking")]
    if blocking and not a.force:
        print("\nHELD for sign-off — answer these before workers spin up:")
        for q in blocking:
            print(f"  • {q['question']}")
        print("\nResolve, update the plan, then re-run with --plan "
              f"plans/{plan['plan_id']}.json (or pass --force to seed anyway).")
        return

    # ----- seed -----
    seeded = seed_queue(repo, plan, precedent, planner_model)
    git(repo, "add", "-A", ".tasks", ".decisions", "plans")
    git(repo, "commit", "-m", f"bootstrap {plan['plan_id']}: seed {len(seeded)} tasks")
    git(repo, "push")

    by_tier = {}
    for _, tier, _ in seeded:
        by_tier[tier] = by_tier.get(tier, 0) + 1
    print(f"\nseeded {len(seeded)} tasks from {plan['plan_id']}: "
          + ", ".join(f"{n} {t}" for t, n in by_tier.items()))
    waiting = sum(1 for _, _, dep in seeded if dep)
    print(f"{len(seeded) - waiting} ready now, {waiting} waiting on dependencies.")
    research = [q for q in plan.get("open_questions", []) if not q.get("blocking")]
    if research:
        print("research questions to resolve in-flight: " + "; ".join(q["question"] for q in research))
    print("start workers, e.g.:  python3 worker.py --tier opus --repo .  --executor '<your model cmd>'")


if __name__ == "__main__":
    main()
