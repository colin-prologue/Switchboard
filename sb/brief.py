"""sb brief — the phase review brief (spec §5.2). Markdown for the PR body:
goal, the rich AgDR review profile (confidence, chosen-over-alternatives,
steelman, blast radius, evidence), work delivered with verification verdicts.
Pending-review AgDRs and failures are surfaced up top (HDR-010). Reads the
tracked decisions/ dir and the .switchboard queue — no git, no model calls."""

import os

from sb import store
from sb.paths import LANES


def phase_obj(plan, phase_id):
    for ph in plan["phases"]:
        if ph["phase_id"] == phase_id:
            return ph
    raise KeyError(f"phase {phase_id} not in plan {plan.get('plan_id')}")


def tasks_in_phase(lay, plan_id, phase_id):
    """Real work tasks (not GATE, not verification) as [(lane, task)]."""
    out = []
    for lane in LANES:
        for t in store.list_tasks(lay, lane):
            src = t.get("source", {})
            if (src.get("plan_id") == plan_id
                    and src.get("phase_id") == phase_id
                    and not t["id"].endswith("/GATE")
                    and not t.get("context", {}).get("verifies")):
                out.append((lane, t))
    return out


def verifications_in_phase(lay, plan_id, phase_id):
    """Map target_id -> verification result, for tasks in this phase."""
    out = {}
    for lane in LANES:
        for t in store.list_tasks(lay, lane):
            target = t.get("context", {}).get("verifies")
            if not target:
                continue
            src = t.get("source", {})
            if src.get("plan_id") == plan_id and src.get("phase_id") == phase_id:
                out[target] = t.get("result", {})
    return out


def decisions_in_phase(lay, plan_id, phase_id):
    out = []
    if not os.path.isdir(lay.decisions):
        return out
    for f in sorted(os.listdir(lay.decisions)):
        if not f.endswith(".json"):
            continue
        rec = store.read_json(os.path.join(lay.decisions, f))
        prov = rec.get("provenance", {})
        if prov.get("plan_id") == plan_id and prov.get("phase_id") == phase_id:
            out.append(rec)
    return out


def _render(plan, ph, tasks, verifs, decisions):
    plan_id = plan["plan_id"]
    L = [f"# Review: {plan_id} / {ph['phase_id']} — {ph['name']}", ""]
    L.append(f"**Goal:** {plan.get('goal', '')}")
    if ph.get("intent"):
        L.append(f"**Why this phase:** {ph['intent']}")
    L.append(f"**Gate condition:** {ph.get('gate', {}).get('condition', '—')}")
    L.append("")

    # HDR-010: anything contestable goes to the top so review starts there.
    pending = [d for d in decisions if d.get("status") == "pending-review"]
    failed = [t for lane, t in tasks if lane == "failed"]
    if pending or failed:
        L.append("## Needs your attention")
        for d in pending:
            L.append(f"- ⚠️ AgDR pending review: **{d.get('id')}** "
                     f"{d.get('title', '')} "
                     f"({d.get('confidence', '?')} confidence; "
                     f"blast radius: {d.get('blast_radius', '—')})")
        for t in failed:
            L.append(f"- ✗ failed task: {t['goal']} — "
                     f"{t.get('failure', {}).get('reason', '')}")
        L.append("")

    L.append("## Decisions made")
    if not decisions:
        L.append("_None recorded in this phase._")
    for d in decisions:
        L.append(f"- **{d.get('title', '(untitled)')}** "
                 f"· {d.get('confidence', '?')} confidence · `{d.get('status')}`")
        if d.get("chosen"):
            L.append(f"  - chose **{d['chosen']}**")
        alts = [o["name"] for o in d.get("options", [])
                if o.get("name") != d.get("chosen")]
        if alts:
            L.append(f"  - over: {', '.join(alts)}")
        if d.get("reasoning"):
            L.append(f"  - why: {d['reasoning']}")
        for s in d.get("steelman", []):
            L.append(f"  - steelman ({s.get('option')}): {s.get('strongest_case')}")
        if d.get("blast_radius"):
            L.append(f"  - blast radius: {d['blast_radius']}")
        ev = [e.get("ref") for e in d.get("evidence", [])]
        if ev:
            L.append(f"  - evidence: {', '.join(str(e) for e in ev)}")
        for fb in d.get("feedback", []):
            L.append(f"  - prior note ({fb['author'].get('id')}): {fb['note']}")
    L.append("")

    L.append("## Work delivered")
    if not tasks:
        L.append("_No work tasks in this phase._")
    for lane, t in tasks:
        mark = {"done": "✓", "failed": "✗"}.get(lane, "·")
        L.append(f"- {mark} {t['goal']}")
        res = t.get("result", {})
        if res.get("summary"):
            L.append(f"  - {res['summary']}")
        for e in res.get("evidence", []):
            L.append(f"  - {e.get('kind')}: {e.get('ref')}")
        v = verifs.get(t["id"])
        if v and v.get("verdict"):
            L.append(f"  - verified: {v['verdict']}"
                     + (f" — {v['verdict_notes']}" if v.get("verdict_notes") else ""))
    L.append("")

    L.append(f"**You're approving:** the design and work above, advancing past "
             f"the {ph['name']} gate.")
    L.append(f"\n_Stamp it:_ `sb stamp --plan {plan_id} --phase {ph['phase_id']} "
             f"--action approve|revise|flag --note \"...\"`")
    return "\n".join(L)


def build_brief(lay, plan, phase_id):
    ph = phase_obj(plan, phase_id)
    plan_id = plan["plan_id"]
    tasks = tasks_in_phase(lay, plan_id, phase_id)
    verifs = verifications_in_phase(lay, plan_id, phase_id)
    decisions = decisions_in_phase(lay, plan_id, phase_id)
    return _render(plan, ph, tasks, verifs, decisions)
