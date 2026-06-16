"""sb stamp — records the human's gate verdict (spec §5.2) and, on approve,
completes the phase GATE task (paused -> done), which unblocks the next phase
(its tasks depend on that GATE id). Engine-pure: NO git operations. The human
merges the PR; stamp records the ratification and frees the queue.

Touches ONLY phase decisions (filtered by provenance) plus the one HDR it
writes — never the hand-authored architecture HDRs."""

import datetime as dt
import os
import re

from sb import store, validate
from sb.brief import decisions_in_phase, tasks_in_phase


class GateNotReady(Exception):
    pass


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def next_hdr_id(lay):
    hi = 0
    if os.path.isdir(lay.decisions):
        for f in os.listdir(lay.decisions):
            m = re.match(r"HDR-(\d+)", f)
            if m:
                hi = max(hi, int(m.group(1)))
    return f"HDR-{hi + 1:03d}"


def gate_ready(lay, plan_id, phase_id):
    """Every real work task in the phase has reached done (none queued/active/
    paused/failed). Verification and GATE tasks are excluded by tasks_in_phase."""
    tasks = tasks_in_phase(lay, plan_id, phase_id)
    return bool(tasks) and all(lane == "done" for lane, _ in tasks)


_STATUS_ON_APPROVE = {True: "feedback-incorporated", False: "approved"}


def stamp(lay, plan_id, phase_id, action, note="", reviewer="colin",
          target=None, force=False):
    if action == "approve" and not force and not gate_ready(lay, plan_id, phase_id):
        raise GateNotReady(
            f"{plan_id}/{phase_id} is not gate-ready (work tasks unfinished); "
            f"pass force=True to stamp anyway")

    author = {"kind": "human", "id": reviewer, "role": "reviewer"}
    fb = {"author": author, "timestamp": now_iso(), "action": action,
          "note": note or f"Gate {action}."}

    touched = []
    for d in decisions_in_phase(lay, plan_id, phase_id):
        if target and d.get("id") != target:
            continue
        d.setdefault("feedback", []).append(fb)
        if action == "approve":
            d["status"] = _STATUS_ON_APPROVE[bool(note)]
        elif action == "revise":
            d["status"] = "proposed"
        elif action == "flag":
            d["status"] = "pending-review"
        validate.check("decision", d)
        store.write_json(os.path.join(lay.decisions, f"{d['id']}.json"), d)
        touched.append(d["id"])

    hdr_id = next_hdr_id(lay)
    hdr = {
        "schema_version": "0.3.0", "id": hdr_id, "type": "human",
        "status": "approved" if action == "approve" else "proposed",
        "timestamp": now_iso(), "level": "feature", "tags": ["gate-review"],
        "author": author,
        "title": (f"Gate review: {plan_id}/{phase_id} — {action}")[:140],
        "reasoning": note or f"Gate {action} with no additional note.",
        "provenance": {"plan_id": plan_id, "phase_id": phase_id},
        "depends_on": touched,
    }
    validate.check("decision", hdr)
    store.write_json(os.path.join(lay.decisions, f"{hdr_id}.json"), hdr)

    gate_id = f"{plan_id}/{phase_id}/GATE"
    advanced = False
    if action == "approve":
        lane, gate = store.find_task(lay, gate_id)
        if lane == "paused":
            gate["status"] = "done"
            gate["result"] = {
                "schema_version": "0.1.0", "outcome": "success",
                "summary": f"Gate approved by {reviewer}. {note}".strip(),
                "completed_at": now_iso()}
            # write-before-move invariant (see sb/results.py): finalize the body
            # while still in paused/, then atomically rename into done/.
            store.write_task(lay, "paused", gate)
            advanced = store.move_task(lay, "paused", "done", gate_id)

    return {"action": action, "hdr": hdr_id, "touched": touched,
            "gate_id": gate_id, "gate_advanced": advanced}
