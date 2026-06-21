"""file-result: the only door results enter through. Routes by outcome,
enqueues the verification lane (PHI-030: only a verifier verdict reaches
done), and applies verdicts back to targets.

Known race: the verify task is enqueued before its author moves to
paused/awaiting_verification. A verifier that claims and files in that gap
gets a retryable ValueError and recovers via the stale sweep. The reverse
order would risk a silently-stuck author (paused with no verify task and
no sweeper), which is worse."""

import datetime as dt
import os

from sb import leases, store, validate


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def result_path(lay, task_id):
    return os.path.join(lay.results, store.fname(task_id))


def verifier_tier_for(author_tier, cfg):
    vt = cfg.get("verifier_tier", "sonnet")
    return cfg.get("verifier_tier_fallback", "opus") if vt == author_tier else vt


def file_result(lay, cfg, task_id):
    rp = result_path(lay, task_id)
    if not os.path.exists(rp):
        lane, _ = store.find_task(lay, task_id)
        if lane not in (None, "active"):
            raise FileNotFoundError(
                f"no result file at {rp} — task is in {lane}; already filed?")
        raise FileNotFoundError(f"no result file at {rp}")
    result = validate.check("result", store.read_json(rp))
    lane, task = store.find_task(lay, task_id)
    if lane != "active":
        where = f"lane={lane}" if lane else "not found in any lane"
        raise ValueError(f"{task_id} is not active ({where})")
    result.setdefault("completed_at", now_iso())
    task["result"] = result

    target_id = task.get("context", {}).get("verifies")
    if target_id:
        dest = _apply_verdict(lay, cfg, task, result, target_id)
    else:
        dest = _route_outcome(lay, cfg, task, result)
    # Body finalized BEFORE the rename: once a file lands in queued/ it is
    # instantly claimable, so no write may follow the move (ghost-task race —
    # same invariant as claims.requeue_stale).
    store.write_task(lay, "active", task)
    if not store.move_task(lay, "active", dest, task_id):
        raise ValueError(f"{task_id} vanished from active while filing (swept?)")
    leases.clear_lease(lay, task_id)
    os.remove(rp)
    return dest


def block(lay, cfg, task_id, reason):
    """Force a task to paused_for_human via a synthesized `blocked` result.

    The deny->blocked contract (sub-plan B §3): when a dispatched subagent
    returns with NO valid result file — a guard-forced stop for rabbit-trailing,
    or a crash — the worker calls this so the task pauses for human review
    instead of `file_result` raising FileNotFoundError. The guard hook only
    denies + nudges; outcome semantics stay here in the engine.

    Reuses file_result's routing (DRY, single validated path): synthesize the
    blocked result on disk, then file it normally. A crashed *verifier* is infra,
    not human-blockable — reject it and let the caller `release` instead."""
    rp = result_path(lay, task_id)
    if os.path.exists(rp):
        raise ValueError(
            f"{task_id} already has a result file at {rp}; file it with "
            f"file-result rather than synthesizing a block")
    lane, task = store.find_task(lay, task_id)
    if lane != "active":
        where = f"lane={lane}" if lane else "not found in any lane"
        raise ValueError(f"{task_id} is not active ({where}); cannot block")
    if task.get("context", {}).get("verifies"):
        raise ValueError(
            f"{task_id} is a verification task; a crashed verifier is infra — "
            f"release it for another verifier, do not block")
    store.write_json(rp, {"schema_version": "0.2.0", "outcome": "blocked",
                          "summary": reason or "subagent returned no result file"})
    return file_result(lay, cfg, task_id)


def _route_outcome(lay, cfg, task, result):
    outcome = result["outcome"]
    if outcome == "success":
        task["status"] = "awaiting_verification"
        _enqueue_verification(lay, cfg, task)
        return "paused"
    if outcome == "blocked":
        task["status"] = "paused_for_human"
        return "paused"
    return _requeue_or_fail(lay, cfg, task, f"outcome={outcome}")


def _requeue_or_fail(lay, cfg, task, note):
    task.setdefault("context", {}).setdefault("prior_attempts", []) \
        .append(task.pop("result"))
    task["attempts"] = task.get("attempts", 0) + 1
    task.pop("claim", None)
    if task["attempts"] >= cfg.get("max_attempts", 3):
        task["status"] = "failed"
        task["failure"] = {"reason": note}
        return "failed"
    task["status"] = "queued"
    return "queued"


def _enqueue_verification(lay, cfg, task):
    vid = f"{task['id']}.V{task.get('attempts', 0) + 1}"
    verify = {
        "schema_version": "0.2.0",
        "id": vid,
        "tier": verifier_tier_for(task.get("tier"), cfg),
        "status": "queued",
        "source": task.get("source", {}),
        "goal": f"Verify: {task['goal']}",
        "context": {
            "repo_state": task.get("context", {}).get("repo_state", "HEAD"),
            "branch": task.get("context", {}).get("branch", ""),
            "chain_depth": task.get("context", {}).get("chain_depth", 0),
            "verifies": task["id"],
            "depends_on": [],
        },
        "done": task["done"],
        "attempts": 0,
        "created_at": now_iso(),
        "created_by": "sb",
    }
    store.write_task(lay, "queued", verify)


def _apply_verdict(lay, cfg, vtask, result, target_id):
    verdict = result.get("verdict")
    if verdict not in ("pass", "fail"):
        raise ValueError("verification result must set verdict: pass|fail")
    lane, target = store.find_task(lay, target_id)
    if lane != "paused" or target.get("status") != "awaiting_verification":
        raise ValueError(f"{target_id} is not awaiting verification (lane={lane})")

    if verdict == "pass":
        target["status"] = "done"
        dest = "done"
    else:
        prior = target.pop("result", None) or {}
        prior["verifier_notes"] = result.get("verdict_notes", "verification failed")
        target.setdefault("context", {}).setdefault("prior_attempts", []).append(prior)
        target["attempts"] = target.get("attempts", 0) + 1
        target.pop("claim", None)
        if target["attempts"] >= cfg.get("max_attempts", 3):
            target["status"] = "failed"
            target["failure"] = {"reason": f"verification failed: "
                                           f"{prior['verifier_notes']}"}
            dest = "failed"
        else:
            target["status"] = "queued"
            dest = "queued"
    # Same write-before-move invariant: update the body while the target is
    # still in paused/ (un-claimable), then rename. Never write after a move.
    store.write_task(lay, "paused", target)
    if not store.move_task(lay, "paused", dest, target_id):
        raise ValueError(f"{target_id} vanished from paused while applying verdict")
    vtask["status"] = "done"
    return "done"  # the verification task itself always completes
