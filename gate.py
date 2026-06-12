#!/usr/bin/env python3
"""gate.py — the human-review gate surface (the oversight loop).

Three subcommands:
  status  — is this phase's gate ready for review? (tasks done, decisions settled)
  brief   — generate the scannable review brief a reviewer reads in ~90 seconds
  stamp   — record the reviewer's verdict: append feedback to the phase's decisions,
            write a human decision record (HDR) capturing the gate outcome, and on
            approval complete the gate so the next phase's tasks become claimable.

Enforcement reuses the queue: bootstrap seeds a GATE placeholder task (in the `paused`
lane) for each human gate, and the next phase's tasks depend on it. Workers only claim
tasks whose dependencies are all `done`, so the next phase stays blocked until `stamp
--approve` moves the GATE task to `done`. No worker changes needed.
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)


def load_plan(repo, plan_id):
    p = os.path.join(repo, "plans", f"{plan_id}.json")
    if not os.path.exists(p):
        sys.exit(f"no plan at {p}")
    return read(p)


def phase_obj(plan, phase_id):
    for ph in plan["phases"]:
        if ph["phase_id"] == phase_id:
            return ph
    sys.exit(f"phase {phase_id} not in plan")


def tasks_in_phase(repo, plan_id, phase_id):
    """Return [(lane, task)] for real tasks (not the GATE placeholder) in this phase."""
    out = []
    for lane in ["queued", "active", "paused", "done", "failed"]:
        d = os.path.join(repo, ".tasks", lane)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            t = read(os.path.join(d, f))
            src = t.get("source", {})
            if src.get("plan_id") == plan_id and src.get("phase_id") == phase_id \
                    and not t["id"].endswith("/GATE"):
                out.append((lane, t))
    return out


def decisions_in_phase(repo, plan_id, phase_id):
    out = []
    d = os.path.join(repo, ".decisions")
    if os.path.isdir(d):
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            rec = read(os.path.join(d, f))
            prov = rec.get("provenance", {})
            if prov.get("plan_id") == plan_id and prov.get("phase_id") == phase_id:
                out.append(rec)
    return out


def gate_checks(tasks, decisions):
    lanes = [lane for lane, _ in tasks]
    checks = {
        "all_tasks_done": bool(tasks) and all(l == "done" for l in lanes),
        "no_failed_tasks": "failed" not in lanes,
        "no_pending_decisions": all(d.get("status") != "pending-review" for d in decisions),
    }
    checks["ready"] = all(checks.values())
    return checks


def next_id(repo, prefix):
    hi = 0
    d = os.path.join(repo, ".decisions")
    if os.path.isdir(d):
        for f in os.listdir(d):
            m = re.search(rf"{prefix}-(\d+)", f)
            if m:
                hi = max(hi, int(m.group(1)))
    return f"{prefix}-{hi + 1:03d}"


# ------------------------------------------------------------------ status
def cmd_status(repo, plan_id, phase_id):
    plan = load_plan(repo, plan_id)
    phases = [phase_obj(plan, phase_id)] if phase_id else plan["phases"]
    for ph in phases:
        tasks = tasks_in_phase(repo, plan_id, ph["phase_id"])
        decisions = decisions_in_phase(repo, plan_id, ph["phase_id"])
        c = gate_checks(tasks, decisions)
        gate = ph.get("gate", {})
        kind = gate.get("type", "auto")
        done = sum(1 for l, _ in tasks if l == "done")
        flag = "READY" if c["ready"] else "not ready"
        print(f"{ph['phase_id']} {ph['name']}  [{kind} gate]  {flag}")
        print(f"    tasks {done}/{len(tasks)} done | "
              f"pending decisions: {sum(1 for d in decisions if d.get('status')=='pending-review')} | "
              f"failed: {sum(1 for l,_ in tasks if l=='failed')}")
        if kind == "human" and c["ready"]:
            print(f"    -> run:  gate.py brief --plan {plan_id} --phase {ph['phase_id']}")


# ------------------------------------------------------------------- brief
def build_brief(plan, ph, tasks, decisions):
    L = [f"# Review: {plan['plan_id']} / {ph['phase_id']} — {ph['name']}", ""]
    L.append(f"**Goal:** {plan.get('goal','')}")
    if ph.get("intent"):
        L.append(f"**Why this phase:** {ph['intent']}")
    L.append(f"**Gate condition:** {ph.get('gate',{}).get('condition','—')}")
    L.append("")

    L.append("## Decisions made")
    if not decisions:
        L.append("_None recorded in this phase._")
    for d in decisions:
        L.append(f"- **{d.get('title','(untitled)')}**  · {d.get('confidence','?')} confidence · `{d.get('status')}`")
        if d.get("chosen"):
            L.append(f"  - chose **{d['chosen']}**")
        opts = [o["name"] for o in d.get("options", []) if o.get("name") != d.get("chosen")]
        if opts:
            L.append(f"  - over: {', '.join(opts)}")
        if d.get("reasoning"):
            L.append(f"  - why: {d['reasoning'][:200]}")
        ev = [e.get("ref") for e in d.get("evidence", [])]
        if ev:
            L.append(f"  - evidence: {', '.join(ev)}")
        for fb in d.get("feedback", []):
            L.append(f"  - prior note ({fb['author'].get('id')}): {fb['note']}")
    L.append("")

    L.append("## Work delivered")
    for lane, t in tasks:
        res = t.get("result", {})
        mark = {"done": "✓", "failed": "✗"}.get(lane, "·")
        L.append(f"- {mark} {t['goal']}")
        if res.get("summary"):
            L.append(f"  - {res['summary']}")
        for e in res.get("evidence", []):
            L.append(f"  - {e.get('kind')}: {e.get('ref')}")
    L.append("")

    pend = [d for d in decisions if d.get("status") == "pending-review"]
    failed = [t for l, t in tasks if l == "failed"]
    if pend or failed:
        L.append("## Needs your attention")
        for d in pend:
            L.append(f"- decision awaiting review: {d.get('title')}")
        for t in failed:
            L.append(f"- failed task: {t['goal']} — {t.get('failure',{}).get('reason','')}")
        L.append("")

    L.append(f"**You're approving:** the design and work above, advancing past the {ph['name']} gate.")
    L.append(f"\n_Stamp it:_ `gate.py stamp --plan {plan['plan_id']} --phase {ph['phase_id']} "
             f"--action approve|revise|flag --note \"...\"`")
    return "\n".join(L)


def cmd_brief(repo, plan_id, phase_id):
    plan = load_plan(repo, plan_id)
    ph = phase_obj(plan, phase_id)
    tasks = tasks_in_phase(repo, plan_id, phase_id)
    decisions = decisions_in_phase(repo, plan_id, phase_id)
    md = build_brief(plan, ph, tasks, decisions)
    os.makedirs(os.path.join(repo, "reviews"), exist_ok=True)
    out = os.path.join(repo, "reviews", f"{plan_id}_{phase_id}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print(f"\n(written to {out})")


# ------------------------------------------------------------------- stamp
def cmd_stamp(repo, plan_id, phase_id, action, note, reviewer, target):
    plan = load_plan(repo, plan_id)
    ph = phase_obj(plan, phase_id)
    decisions = decisions_in_phase(repo, plan_id, phase_id)
    author = {"kind": "human", "id": reviewer, "role": "EM"}
    stamp = {"author": author, "timestamp": now(), "action": action, "note": note}

    touched = []
    for d in decisions:
        if target and d.get("id") != target:
            continue
        d.setdefault("feedback", []).append(stamp)
        if action == "approve":
            d["status"] = "feedback-incorporated" if note else "approved"
        elif action == "revise":
            d["status"] = "proposed"
        write(os.path.join(repo, ".decisions", f"{d['id']}.json"), d)
        touched.append(d["id"])

    # record the gate review itself as a human decision record
    hdr_id = next_id(repo, "HDR")
    hdr = {
        "schema_version": "0.2.0", "id": hdr_id, "type": "human",
        "status": "approved" if action == "approve" else "proposed",
        "timestamp": now(), "phase": ph.get("name"), "level": "feature",
        "tags": ["gate-review"], "author": author,
        "title": f"Gate review: {ph['name']} — {action}",
        "reasoning": note or f"Gate {action} with no additional note.",
        "provenance": {"plan_id": plan_id, "phase_id": phase_id},
        "depends_on": touched,
    }
    write(os.path.join(repo, ".decisions", f"{hdr_id}.json"), hdr)

    advanced = False
    if action == "approve":
        gate_file = None
        pdir = os.path.join(repo, ".tasks", "paused")
        for f in (os.listdir(pdir) if os.path.isdir(pdir) else []):
            if not f.endswith(".json"):
                continue
            if read(os.path.join(pdir, f)).get("id") == f"{plan_id}/{phase_id}/GATE":
                gate_file = f
        if gate_file:
            t = read(os.path.join(pdir, gate_file))
            t["status"] = "done"
            t.setdefault("result", {})["outcome"] = "success"
            t["result"]["summary"] = f"Gate approved by {reviewer}. {note}".strip()
            t["result"]["completed_at"] = now()
            write(os.path.join(pdir, gate_file), t)
            shutil.move(os.path.join(pdir, gate_file),
                        os.path.join(repo, ".tasks", "done", gate_file))
            advanced = True

    git(repo, "add", "-A", ".decisions", ".tasks")
    git(repo, "commit", "-m", f"gate {action}: {plan_id}/{phase_id} by {reviewer}")
    git(repo, "push")

    print(f"recorded {action} on {phase_id} ({hdr_id}); feedback on {len(touched)} decision(s).")
    if action == "approve":
        print("gate completed — next phase unblocked." if advanced
              else "approved (no GATE task found to complete; downstream may not be gated).")
    elif action == "revise":
        print("sent back for revision; affected decisions returned to 'proposed'.")


def main():
    ap = argparse.ArgumentParser(description="Human-review gate surface.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ["status", "brief", "stamp"]:
        s = sub.add_parser(name)
        s.add_argument("--repo", default=".")
        s.add_argument("--plan", required=True, dest="plan_id")
        s.add_argument("--phase", dest="phase_id", required=(name != "status"))
        if name == "stamp":
            s.add_argument("--action", required=True, choices=["approve", "revise", "flag"])
            s.add_argument("--note", default="")
            s.add_argument("--reviewer", default="colin")
            s.add_argument("--decision", dest="target", default="", help="target one decision id; default = all in phase")
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)

    if a.cmd == "status":
        cmd_status(repo, a.plan_id, a.phase_id)
    elif a.cmd == "brief":
        cmd_brief(repo, a.plan_id, a.phase_id)
    elif a.cmd == "stamp":
        cmd_stamp(repo, a.plan_id, a.phase_id, a.action, a.note, a.reviewer, a.target)


if __name__ == "__main__":
    main()
