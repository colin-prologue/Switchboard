# sb Operator Surfaces Implementation Plan (M0, Plan 2 of 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the operator-facing read side of the `sb` engine — the status digest (`sb status --emit`), the phase review brief (`sb brief`), the gate stamp that completes the phase GATE and unblocks the next phase (`sb stamp`), and the edge-triggered notify hook — fully unit-tested, per the v2 design (`docs/specs/2026-06-12-switchboard-v2-design.md` §5.2, §7) and HDR-010.

**Architecture:** Four new focused modules behind the existing argparse CLI — `sb/digest.py` (pure function of disk state → the read-side primitive), `sb/brief.py` (markdown for the PR body), `sb/stamp.py` (records the human verdict + completes the GATE task), and `sb/notify.py` + `sb/channels.py` (pluggable notification). One new contract, `schemas/digest.schema.json`, enforced at the digest boundary. The engine stays deterministic and git-free: `sb stamp` mutates queue + decision files and frees the next phase, but the human merges the PR; stamp records the ratification, it does not perform it.

**Tech Stack:** Python 3.11+, `jsonschema`, `pytest`. No new dependencies (macOS notification uses `osascript` via `subprocess`).

**Scope notes:** Plan 1 (`docs/plans/2026-06-12-sb-engine-core.md`, EXECUTED) built the engine: lanes, leases, claims/wait, DAG-guarded spawn, the verification lane, seeding, query, CLI. Plan 3 covers the `/sb-work` skill, subagent prompt protocols, tripwire hooks, and the M0 end-to-end exit bar. This plan is the read/oversight surface between them.

**HDR-010 boundary (read before starting):** HDR-010 requires that tier judgment eventually come from an *independent fresh-context agent* (verifier-class), not self-assessment. That judge is **Plan 3** (worker-skill escalation logic + the M0 exit bar exercises it). Plan 2's HDR-010 obligation is narrower and explicit, taken verbatim from HDR-010's `blast_radius` field: *"pending-review AgDRs must route through the notification digest."* So in this plan: the digest **carries** pending-review AgDRs (the tier-2 ping payload), the notify hook **fires** on newly-pending ones, and the brief **surfaces** them at the top. We build the channel; Plan 3 builds the judge that decides what flows through it.

---

## Background the implementer needs

You are extending an existing, fully-tested engine. **Do not reinvent its primitives — import them.** The relevant interfaces (all already on disk and green):

- `sb.paths` — `Layout(repo)` with attributes `.repo .root .tasks .leases .heartbeats .results .config_path .decisions .plans`, method `.lane(name)`; module-level `LANES = ["queued","active","paused","done","failed"]`; `init(repo) -> Layout`; `load_config(lay) -> dict` (merges `DEFAULT_CONFIG`). `DEFAULT_CONFIG` has `verifier_tier, verifier_tier_fallback, max_attempts, lease_ttl_s (5400), max_chain_depth`.
- `sb.store` — `read_json(path)`, `write_json(path, obj)` (atomic), `fname(task_id)` (`/`→`_`, `+".json"`), `task_path(lay, lane, id)`, `write_task(lay, lane, task)` (validates), `list_tasks(lay, lane) -> [task]` (sorted), `move_task(lay, src, dst, id) -> bool` (atomic rename; False = lost race), `find_task(lay, id) -> (lane|None, task|None)`, `done_ids(lay) -> set`.
- `sb.validate` — `check(name, obj)` raises `ValueError` on schema failure. `NAMES` maps `"task"|"plan"|"decision"|"result"` to schema filenames. **You will add `"digest"` here in Task 1.**
- `sb.leases` — `read_lease(lay, id) -> dict|None` (`{task_id, worker_id, claimed_at, ttl_s}`), `is_expired(lease, now=None) -> bool` (epoch seconds; `now > claimed_at + ttl_s`).
- `sb.claims` — `claim_one(lay, worker_id, tier=None, cfg=None)`, `deps_met(task, completed_set)`, `heartbeat(lay, worker_id)` (writes `.switchboard/heartbeats/<id>.json` = `{worker_id, at}` where `at` is epoch `time.time()`).
- `sb.seed.seed(lay, plan, repo_state="HEAD", force=False) -> [task_id]` — expands a plan into the queue; **every phase ends at a `<plan>/<phase>/GATE` task created in the `paused` lane with `status="paused_for_human"`**, and the next phase's tasks `depends_on` that GATE id. Completing the GATE (→ `done` lane) is what unblocks the next phase. **This plan's `sb stamp` is the thing that completes it.**

Decision records (AgDRs `ADR-*`, human `HDR-*`, synthesis `SDR-*`) live in the tracked top-level `decisions/` directory, validated by `schemas/decision-record.schema.json` (v0.3.0). A record links to its originating phase via the optional `provenance: {plan_id, phase_id, task_id}` block. **Phase AgDRs carry `provenance`; the hand-authored architecture HDRs (HDR-001..010) do not** — so filtering by provenance naturally includes the former and excludes the latter. The schema's `status` enum includes `pending-review` (the HDR-010 tier-2 state) and `feedback-incorporated`. `confidence` is `high|medium|low`; `blast_radius` is a free string; `steelman` is an array of `{option, strongest_case}`.

The v1 `gate.py` at the repo root is the direct ancestor of `sb brief`/`sb stamp` — read it for the shape, but it targets the v1 layout (`.tasks/`, `.decisions/`) and does `git add/commit/push`. **The v2 versions read the v2 layout via `store`, and do NO git operations** (spec §4.2: orchestration state never appears on a branch; the PR is the gate, the human merges it). `gate.py` is deleted in Task 7.

---

## File structure

```
schemas/digest.schema.json     # NEW: the status-digest contract (v0.1.0)
sb/validate.py                 # MODIFY: register "digest" in NAMES
sb/digest.py                   # NEW: build_digest — read-side primitive (spec §7)
sb/brief.py                    # NEW: phase review brief markdown (spec §5.2)
sb/stamp.py                    # NEW: gate verdict + GATE-task completion
sb/channels.py                 # NEW: pluggable notification channels
sb/notify.py                   # NEW: edge-triggered notify hook
hooks/sb_notify.py             # NEW: thin Claude Code hook shim -> sb notify
sb/cli.py                      # MODIFY: add brief/stamp/status/notify subcommands
tests/test_digest.py          # NEW
tests/test_brief.py           # NEW
tests/test_stamp.py           # NEW
tests/test_notify.py          # NEW
tests/test_cli_operator.py    # NEW: operator-surface CLI smoke
tests/helpers.py              # MODIFY: add make_agdr factory
```

Convention notes for the implementer:
- The digest is **machine-first JSON**, validated against its schema on every build (spec §2: jsonschema enforcement at read/write boundaries). The brief is **human-first markdown** printed to stdout for piping into a PR body — it is the one `sb` output that is not JSON.
- `sb brief`/`sb stamp` take `--plan <PLAN-ID>` (a plan *id* looked up under `plans/<id>.json`), unlike `sb seed` which takes `--plan <path>` (a file to ingest). They operate on an already-seeded plan; seed ingests a new one. Document this where it could confuse.
- The notify hook is **edge-triggered**: it fires only on items that are new since its last run, comparing the live digest against `.switchboard/notify-state.json`. This is what lets the worker loop poll it every iteration without spamming.
- `sb stamp` does NOT validate or touch legacy `decisions/` records: it filters strictly by `provenance.{plan_id, phase_id}`, so the only records it writes are 0.3.0 phase AgDRs plus the new 0.3.0 HDR it creates.

---

### Task 1: Digest schema + validation registration

**Files:**
- Create: `schemas/digest.schema.json`
- Modify: `sb/validate.py` (one line in `NAMES`)
- Test: `tests/test_digest_schema.py`

- [ ] **Step 1: Write the failing test**

`tests/test_digest_schema.py`:
```python
import json

import pytest
from jsonschema import Draft202012Validator

from sb import validate


def test_digest_registered_in_validate():
    assert validate.NAMES["digest"] == "digest.schema.json"


GOOD_DIGEST = {
    "schema_version": "0.1.0",
    "generated_at": "2026-06-14T00:00:00+00:00",
    "lanes": {"queued": 1, "active": 0, "paused": 2, "done": 3, "failed": 0},
    "gates_ready": [{"id": "PLAN-001/PH-1/GATE", "condition": "phase PR merged"}],
    "paused_for_human": [{"id": "PLAN-001/PH-1/T-9", "reason": "missing credential"}],
    "pending_agdrs": [{
        "id": "ADR-051", "title": "Use immutable snapshots",
        "confidence": "medium", "blast_radius": "cache module only",
        "provenance": {"plan_id": "PLAN-001", "phase_id": "PH-1"},
    }],
    "stale_workers": [{"worker_id": "w1", "last_seen_s_ago": 9000}],
    "stale_active": [{"id": "PLAN-001/PH-1/T-1.V1", "verifies": "PLAN-001/PH-1/T-1"}],
    "quota": {"state": "ok"},
}


def test_digest_schema_accepts_valid():
    validate.check("digest", GOOD_DIGEST)


def test_digest_schema_accepts_nulls_and_empty():
    d = dict(GOOD_DIGEST,
             gates_ready=[], paused_for_human=[], stale_workers=[],
             stale_active=[{"id": "x", "verifies": None}],
             pending_agdrs=[{"id": "ADR-052", "title": None, "confidence": None,
                             "blast_radius": None, "provenance": {}}])
    validate.check("digest", d)


def test_digest_schema_rejects_unknown_field():
    with pytest.raises(ValueError):
        validate.check("digest", dict(GOOD_DIGEST, surprise=1))


def test_digest_schema_rejects_bad_quota_shape():
    with pytest.raises(ValueError):
        validate.check("digest", dict(GOOD_DIGEST, quota="ok"))
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_digest_schema.py -v`
Expected: FAIL — `KeyError: 'digest'` in `validate.NAMES` / schema file missing.

- [ ] **Step 3: Create `schemas/digest.schema.json`**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://colin-prologue/schemas/digest/v0.1.0.json",
  "title": "Status Digest",
  "description": "The read-side primitive (spec §7): a pure snapshot of orchestration state for the notify hook and the future nexus. Produced by sb status --emit. Carries pending-review AgDRs so HDR-010 tier-2 escalations reach the human through the notification channel.",
  "type": "object",
  "additionalProperties": false,
  "required": ["schema_version", "generated_at", "lanes", "gates_ready",
               "paused_for_human", "pending_agdrs", "stale_workers",
               "stale_active", "quota"],
  "properties": {
    "schema_version": { "const": "0.1.0" },
    "generated_at": { "type": "string", "format": "date-time" },
    "lanes": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "queued": { "type": "integer", "minimum": 0 },
        "active": { "type": "integer", "minimum": 0 },
        "paused": { "type": "integer", "minimum": 0 },
        "done": { "type": "integer", "minimum": 0 },
        "failed": { "type": "integer", "minimum": 0 }
      }
    },
    "gates_ready": {
      "type": "array",
      "description": "GATE tasks whose dependencies are all done — ready for sb stamp.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id"],
        "properties": {
          "id": { "type": "string" },
          "condition": { "type": ["string", "null"] }
        }
      }
    },
    "paused_for_human": {
      "type": "array",
      "description": "Non-gate tasks escalated to a human (blocked/hard-escalation/depth cap).",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id"],
        "properties": {
          "id": { "type": "string" },
          "reason": { "type": ["string", "null"] }
        }
      }
    },
    "pending_agdrs": {
      "type": "array",
      "description": "HDR-010 tier-2: AgDRs filed status=pending-review, pushed through this channel while work proceeds.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id"],
        "properties": {
          "id": { "type": "string" },
          "title": { "type": ["string", "null"] },
          "confidence": { "type": ["string", "null"] },
          "blast_radius": { "type": ["string", "null"] },
          "provenance": { "type": "object" }
        }
      }
    },
    "stale_workers": {
      "type": "array",
      "description": "Heartbeats older than lease TTL — the fleet-stalled signal.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["worker_id"],
        "properties": {
          "worker_id": { "type": ["string", "null"] },
          "last_seen_s_ago": { "type": "integer", "minimum": 0 }
        }
      }
    },
    "stale_active": {
      "type": "array",
      "description": "Active tasks with an expired/missing lease — where malformed-verdict verify tasks land before the stale sweep requeues them (results.py carry-over).",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id"],
        "properties": {
          "id": { "type": "string" },
          "verifies": { "type": ["string", "null"] }
        }
      }
    },
    "quota": {
      "type": "object",
      "additionalProperties": false,
      "description": "Quota/usage-cap state — ADVISORY ONLY, never a claim gate (HDR-011). Populated in Plan 3 by a deterministic detector (a PostToolUse hook that catches rate-limit/429 signals token-free), NOT by reasoning in a throttled session; optionally enriched from Claude Code OTEL token counters. Absent => {state: ok}. The engine never sets it and never reads it to block a claim.",
      "required": ["state"],
      "properties": {
        "state": { "type": "string", "examples": ["ok", "throttled", "exhausted"] },
        "detail": { "type": "string" },
        "retry_after_s": { "type": "integer", "minimum": 0 }
      }
    }
  }
}
```

- [ ] **Step 4: Register `"digest"` in `sb/validate.py`**

In `sb/validate.py`, the `NAMES` dict currently reads:
```python
NAMES = {
    "task": "task.schema.json",
    "plan": "plan.schema.json",
    "decision": "decision-record.schema.json",
    "result": "result.schema.json",
}
```
Add one entry so it reads:
```python
NAMES = {
    "task": "task.schema.json",
    "plan": "plan.schema.json",
    "decision": "decision-record.schema.json",
    "result": "result.schema.json",
    "digest": "digest.schema.json",
}
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_digest_schema.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add schemas/digest.schema.json sb/validate.py tests/test_digest_schema.py
git commit -m "feat(schemas): status-digest contract v0.1.0, registered in validate"
```

---

### Task 2: `sb/digest.py` — the read-side primitive

**Files:**
- Create: `sb/digest.py`
- Modify: `tests/helpers.py` (add `make_agdr`)
- Test: `tests/test_digest.py`

- [ ] **Step 1: Add the AgDR factory to `tests/helpers.py`**

Append to the existing `tests/helpers.py` (keep `make_task` unchanged):
```python
def make_agdr(rec_id="ADR-051", status="pending-review", plan_id="PLAN-001",
              phase_id="PH-1", **over):
    """Schema-valid (decision v0.3.0) AgDR for brief/stamp/digest tests."""
    rec = {
        "schema_version": "0.3.0",
        "id": rec_id,
        "type": "agent",
        "status": status,
        "timestamp": "2026-06-14T00:00:00+00:00",
        "level": "component",
        "tags": ["caching"],
        "author": {"kind": "model", "id": "claude-opus-4-8", "role": "coder"},
        "title": "Use immutable snapshots for the cache",
        "context": "Concurrency model had to be chosen before implementation.",
        "options": [
            {"name": "immutable-snapshots", "rationale": "no locks"},
            {"name": "mutable-with-locks", "rationale": "lower memory"},
        ],
        "chosen": "immutable-snapshots",
        "confidence": "medium",
        "reasoning": "Snapshots remove the lock-contention failure mode entirely.",
        "steelman": [{"option": "mutable-with-locks",
                      "strongest_case": "Lower memory; a familiar pattern."}],
        "blast_radius": "Cache module only; no public API change.",
        "evidence": [{"kind": "test", "ref": "tests/test_cache.py", "result": "pass"}],
        "provenance": {"plan_id": plan_id, "phase_id": phase_id, "task_id": "T-1"},
    }
    rec.update(over)
    return rec


def put_decision(lay, rec):
    import json
    import os
    with open(os.path.join(lay.decisions, f"{rec['id']}.json"), "w",
              encoding="utf-8") as f:
        json.dump(rec, f)
```

- [ ] **Step 2: Write the failing test**

`tests/test_digest.py`:
```python
import os
import time

from sb import claims, digest, leases, store
from tests.helpers import make_agdr, make_task, put_decision


def test_empty_repo_digest_is_valid_and_quiet(lay):
    cfg = {"lease_ttl_s": 5400}
    d = digest.build_digest(lay, cfg)
    assert d["schema_version"] == "0.1.0"
    assert d["lanes"] == {"queued": 0, "active": 0, "paused": 0,
                          "done": 0, "failed": 0}
    assert d["gates_ready"] == [] and d["pending_agdrs"] == []
    assert d["quota"] == {"state": "ok"}


def test_lane_counts(lay):
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    store.write_task(lay, "done", make_task("PLAN-001/PH-1/T-2", status="done"))
    d = digest.build_digest(lay, {})
    assert d["lanes"]["queued"] == 1 and d["lanes"]["done"] == 1


def test_pending_agdrs_carried(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    put_decision(lay, make_agdr("ADR-052", status="approved"))
    d = digest.build_digest(lay, {})
    ids = [a["id"] for a in d["pending_agdrs"]]
    assert ids == ["ADR-051"]
    assert d["pending_agdrs"][0]["blast_radius"] == "Cache module only; no public API change."


def test_gate_ready_when_deps_done(lay):
    store.write_task(lay, "done", make_task("PLAN-001/PH-1/T-1", status="done"))
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human",
                     context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)
    d = digest.build_digest(lay, {})
    assert [g["id"] for g in d["gates_ready"]] == ["PLAN-001/PH-1/GATE"]


def test_gate_not_ready_when_deps_pending(lay):
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human",
                     context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)
    assert digest.build_digest(lay, {})["gates_ready"] == []


def test_paused_for_human_listed(lay):
    t = make_task("PLAN-001/PH-1/T-9", status="paused_for_human")
    t["failure"] = {"reason": "missing credential"}
    store.write_task(lay, "paused", t)
    d = digest.build_digest(lay, {})
    assert d["paused_for_human"] == [
        {"id": "PLAN-001/PH-1/T-9", "reason": "missing credential"}]


def test_stale_active_surfaces_expired_lease(lay):
    store.write_task(lay, "queued", make_task("PLAN-001/PH-1/T-1"))
    claims.claim_one(lay, "w1", cfg={"lease_ttl_s": 100})
    lease = leases.read_lease(lay, "PLAN-001/PH-1/T-1")
    lease["claimed_at"] -= lease["ttl_s"] + 1
    store.write_json(leases.lease_path(lay, "PLAN-001/PH-1/T-1"), lease)
    d = digest.build_digest(lay, {"lease_ttl_s": 100})
    assert [s["id"] for s in d["stale_active"]] == ["PLAN-001/PH-1/T-1"]


def test_stale_workers_from_heartbeats(lay):
    claims.heartbeat(lay, "w1")
    rec_path = os.path.join(lay.heartbeats, "w1.json")
    rec = store.read_json(rec_path)
    rec["at"] = time.time() - 9000
    store.write_json(rec_path, rec)
    d = digest.build_digest(lay, {"lease_ttl_s": 5400})
    assert d["stale_workers"][0]["worker_id"] == "w1"


def test_quota_read_from_file(lay):
    store.write_json(os.path.join(lay.root, "quota.json"),
                     {"state": "exhausted", "retry_after_s": 600})
    d = digest.build_digest(lay, {})
    assert d["quota"]["state"] == "exhausted"
```

- [ ] **Step 3: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_digest.py -v`
Expected: FAIL — `sb.digest` missing.

- [ ] **Step 4: Implement `sb/digest.py`**

```python
"""Status digest — the read-side primitive (spec §7). A pure function of disk
state, validated against schemas/digest.schema.json. The notify hook and the
future nexus both consume it. Carries pending-review AgDRs so HDR-010 tier-2
escalations reach the human through the notification channel."""

import datetime as dt
import os
import time

from sb import leases, store, validate
from sb.paths import LANES


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _load_decisions(lay):
    out = []
    if not os.path.isdir(lay.decisions):
        return out
    for f in sorted(os.listdir(lay.decisions)):
        if not f.endswith(".json"):
            continue
        try:
            out.append(store.read_json(os.path.join(lay.decisions, f)))
        except (ValueError, OSError):
            continue
    return out


def build_digest(lay, cfg, now=None):
    now = time.time() if now is None else now
    ttl = cfg.get("lease_ttl_s", 5400)

    lanes = {lane: len(store.list_tasks(lay, lane)) for lane in LANES}

    # Active tasks whose lease expired or vanished. This is where a verify task
    # with a malformed (verdict-less) result sits until the stale sweep requeues
    # it (see sb/results.py carry-over note) — surface it here.
    stale_active = []
    for t in store.list_tasks(lay, "active"):
        lease = leases.read_lease(lay, t["id"])
        if lease is None or leases.is_expired(lease, now=now):
            stale_active.append({"id": t["id"],
                                 "verifies": t.get("context", {}).get("verifies")})

    # Heartbeats older than the lease TTL => fleet stalled / silent session death.
    stale_workers = []
    if os.path.isdir(lay.heartbeats):
        for f in sorted(os.listdir(lay.heartbeats)):
            if not f.endswith(".json"):
                continue
            rec = store.read_json(os.path.join(lay.heartbeats, f))
            age = now - rec.get("at", 0)
            if age > ttl:
                stale_workers.append({"worker_id": rec.get("worker_id"),
                                      "last_seen_s_ago": round(age)})

    done = store.done_ids(lay)
    gates_ready, paused_for_human = [], []
    for t in store.list_tasks(lay, "paused"):
        deps = t.get("context", {}).get("depends_on", [])
        if t["id"].endswith("/GATE"):
            if all(d in done for d in deps):
                gates_ready.append({"id": t["id"],
                                    "condition": t.get("done", {}).get("statement")})
        elif t.get("status") == "paused_for_human":
            paused_for_human.append({"id": t["id"],
                                     "reason": t.get("failure", {}).get("reason", "")})

    pending_agdrs = [
        {"id": d.get("id"), "title": d.get("title"),
         "confidence": d.get("confidence"),
         "blast_radius": d.get("blast_radius"),
         "provenance": d.get("provenance", {})}
        for d in _load_decisions(lay)
        if d.get("status") == "pending-review"]

    # ADVISORY status hint only (HDR-011): a deterministic detector (Plan 3
    # PostToolUse hook) writes this token-free on a rate-limit signal; the
    # engine never gates a claim on it. Absent => healthy.
    qpath = os.path.join(lay.root, "quota.json")
    quota = store.read_json(qpath) if os.path.exists(qpath) else {"state": "ok"}

    digest = {
        "schema_version": "0.1.0",
        "generated_at": now_iso(),
        "lanes": lanes,
        "gates_ready": gates_ready,
        "paused_for_human": paused_for_human,
        "pending_agdrs": pending_agdrs,
        "stale_workers": stale_workers,
        "stale_active": stale_active,
        "quota": quota,
    }
    validate.check("digest", digest)
    return digest
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_digest.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add sb/digest.py tests/helpers.py tests/test_digest.py
git commit -m "feat(sb): status digest read-side primitive (carries pending-review AgDRs)"
```

---

### Task 3: `sb/channels.py` + `sb/notify.py` — the edge-triggered notify hook

**Files:**
- Create: `sb/channels.py`
- Create: `sb/notify.py`
- Test: `tests/test_notify.py`

- [ ] **Step 1: Write the failing test**

`tests/test_notify.py`:
```python
from sb import notify, store
from tests.helpers import make_agdr, make_task, put_decision


def collector():
    """A channel that records (title, body) instead of firing a real one."""
    sent = []
    return sent, (lambda title, body: sent.append((title, body)))


def seed_gate_ready(lay):
    store.write_task(lay, "done", make_task("PLAN-001/PH-1/T-1", status="done"))
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human",
                     context={"depends_on": ["PLAN-001/PH-1/T-1"]})
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)


def test_collect_events_groups_by_kind(lay):
    dg = {
        "gates_ready": [{"id": "PLAN-001/PH-1/GATE", "condition": "merged"}],
        "paused_for_human": [{"id": "PLAN-001/PH-1/T-9", "reason": "no creds"}],
        "pending_agdrs": [{"id": "ADR-051", "title": "snapshots",
                           "confidence": "medium"}],
        "stale_workers": [{"worker_id": "w1", "last_seen_s_ago": 9000}],
        "quota": {"state": "exhausted"},
    }
    events = notify.collect_events(dg, seen=[])
    kinds = {e["kind"] for e in events}
    assert kinds == {"gate_ready", "paused_for_human", "pending_agdr",
                     "fleet_stalled", "quota"}


def test_notify_fires_once_then_is_quiet(lay):
    seed_gate_ready(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    sent, ch = collector()
    fired = notify.notify(lay, {"lease_ttl_s": 5400}, channel=ch)
    assert {e["kind"] for e in fired} == {"gate_ready", "pending_agdr"}
    assert len(sent) == 2
    # second run: nothing new -> nothing fires
    sent2, ch2 = collector()
    fired2 = notify.notify(lay, {"lease_ttl_s": 5400}, channel=ch2)
    assert fired2 == [] and sent2 == []


def test_resolved_item_refires_if_it_recurs(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    sent, ch = collector()
    notify.notify(lay, {}, channel=ch)            # fires ADR-051
    # human stamps it -> no longer pending
    rec = make_agdr("ADR-051", status="approved")
    put_decision(lay, rec)
    sent2, ch2 = collector()
    assert notify.notify(lay, {}, channel=ch2) == []   # gone, nothing to fire
    # a NEW pending AgDR appears -> fires (state didn't get stuck)
    put_decision(lay, make_agdr("ADR-052", status="pending-review"))
    sent3, ch3 = collector()
    fired3 = notify.notify(lay, {}, channel=ch3)
    assert [e["kind"] for e in fired3] == ["pending_agdr"]


def test_channels_resolve_known_and_default():
    from sb import channels
    assert channels.resolve("stdout") is channels.stdout
    assert channels.resolve("null")("t", "b") is None
    # unknown name falls back to stdout, never raises
    assert channels.resolve("nope") is channels.stdout
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: FAIL — `sb.notify` / `sb.channels` missing.

- [ ] **Step 3: Implement `sb/channels.py`**

```python
"""Notification channels for the notify hook (spec §7, PHI-029). Each channel
is a callable (title, body) -> None. macOS notification is the default; ntfy /
Teams can be added here later without touching notify.py."""

import json
import subprocess


def macos(title, body):
    # AppleScript string literals are double-quoted; json.dumps gives correct
    # quoting/escaping for the common case. Best-effort: never raise.
    script = (f"display notification {json.dumps(body)} "
              f"with title {json.dumps(title)}")
    subprocess.run(["osascript", "-e", script], check=False,
                   capture_output=True)


def stdout(title, body):
    print(f"[sb notify] {title}: {body}")


def null(title, body):
    return None


def resolve(name):
    return {"macos": macos, "stdout": stdout, "null": null}.get(name, stdout)
```

- [ ] **Step 4: Implement `sb/notify.py`**

```python
"""Notify hook (spec §7, PHI-029). Edge-triggered: fires only on items that are
NEW since the last run, so the worker loop can poll it every iteration without
spamming. Pending-review AgDRs fire here — the HDR-010 tier-2 ping channel.

State lives in .switchboard/notify-state.json as the set of currently-live
notable keys. An item that disappears is dropped from the set, so if it recurs
it fires again; an item still present is not re-fired."""

import os

from sb import channels, store
from sb import digest as digest_mod

STATE_FILE = "notify-state.json"


def _state_path(lay):
    return os.path.join(lay.root, STATE_FILE)


def _load_seen(lay):
    p = _state_path(lay)
    return set(store.read_json(p).get("seen", [])) if os.path.exists(p) else set()


def _key(kind, ident):
    return f"{kind}:{ident}"


def _all_keys(dg):
    """Every notable key in a digest, paired with how to render it."""
    out = []  # (key, kind, title, body)
    for g in dg.get("gates_ready", []):
        out.append((_key("gate_ready", g["id"]), "gate_ready",
                    "Gate ready for review", g["id"]))
    for p in dg.get("paused_for_human", []):
        out.append((_key("paused_for_human", p["id"]), "paused_for_human",
                    "Task paused for human",
                    f"{p['id']} — {p.get('reason') or ''}".strip()))
    for a in dg.get("pending_agdrs", []):
        out.append((_key("pending_agdr", a["id"]), "pending_agdr",
                    "AgDR pending review",
                    f"{a['id']}: {a.get('title') or ''} "
                    f"({a.get('confidence') or '?'} confidence)"))
    for w in dg.get("stale_workers", []):
        out.append((_key("fleet_stalled", w["worker_id"]), "fleet_stalled",
                    "Fleet worker stalled",
                    f"{w['worker_id']} last seen {w.get('last_seen_s_ago')}s ago"))
    state = dg.get("quota", {}).get("state")
    if state not in (None, "ok"):
        out.append((_key("quota", state), "quota", "Quota alert",
                    f"quota state: {state}"))
    return out


def collect_events(dg, seen):
    seen = set(seen)
    return [{"key": k, "kind": kind, "title": title, "body": body}
            for (k, kind, title, body) in _all_keys(dg) if k not in seen]


def notify(lay, cfg, dg=None, channel=None):
    dg = dg if dg is not None else digest_mod.build_digest(lay, cfg)
    seen = _load_seen(lay)
    events = collect_events(dg, seen)
    send = channel or channels.resolve(cfg.get("notify_channel", "macos"))
    for e in events:
        send(e["title"], e["body"])
    # Persist exactly the live set: resolved items drop out (can re-fire later),
    # still-live items stay (won't re-fire).
    live = sorted(k for (k, *_rest) in _all_keys(dg))
    store.write_json(_state_path(lay), {"seen": live})
    return events
```

- [ ] **Step 5: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_notify.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add sb/channels.py sb/notify.py tests/test_notify.py
git commit -m "feat(sb): edge-triggered notify hook with pluggable channels"
```

---

### Task 4: `sb/brief.py` — the phase review brief

**Files:**
- Create: `sb/brief.py`
- Test: `tests/test_brief.py`

- [ ] **Step 1: Write the failing test**

`tests/test_brief.py`:
```python
from sb import brief, store
from tests.helpers import make_agdr, make_task, put_decision

PLAN = {
    "schema_version": "0.1.0", "plan_id": "PLAN-001", "goal": "toy goal",
    "created": "2026-06-14T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "intent": "Decide the model before code.",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [{"task_id": "T-1", "title": "Choose the design",
                    "done": {"statement": "ADR exists"}}]},
    ],
}


def done_task(lay, tid, **over):
    t = make_task(tid, status="done", **over)
    t["result"] = {"schema_version": "0.1.0", "outcome": "success",
                   "summary": "Implemented and tested.",
                   "evidence": [{"kind": "commit", "ref": "abc123"}]}
    store.write_task(lay, "done", t)
    return t


def passed_verify(lay, target_id, notes="looks correct"):
    v = make_task(f"{target_id}.V1", status="done",
                  context={"verifies": target_id})
    v["result"] = {"schema_version": "0.1.0", "outcome": "success",
                   "summary": "verified", "verdict": "pass", "verdict_notes": notes}
    store.write_task(lay, "done", v)


def test_brief_has_header_and_goal(lay):
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "# Review: PLAN-001 / PH-1 — Design" in md
    assert "toy goal" in md
    assert "Decide the model before code." in md


def test_brief_shows_rich_agdr_profile(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "Use immutable snapshots for the cache" in md
    assert "immutable-snapshots" in md            # chosen
    assert "mutable-with-locks" in md             # alternative + steelman
    assert "Lower memory; a familiar pattern." in md   # steelman case
    assert "Cache module only" in md              # blast radius
    assert "medium" in md                         # confidence


def test_brief_surfaces_pending_agdrs_up_top(lay):
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    md = brief.build_brief(lay, PLAN, "PH-1")
    attention = md.index("Needs your attention")
    decisions = md.index("Decisions made")
    assert attention < decisions   # HDR-010: pending review surfaced first
    assert "ADR-051" in md[attention:decisions]


def test_brief_shows_work_with_verdict(lay):
    done_task(lay, "PLAN-001/PH-1/T-1")
    passed_verify(lay, "PLAN-001/PH-1/T-1", notes="stress test green")
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "do the thing" in md                 # goal of make_task
    assert "Implemented and tested." in md
    assert "verified: pass" in md
    assert "stress test green" in md


def test_brief_excludes_gate_and_verify_tasks_from_work(lay):
    gate = make_task("PLAN-001/PH-1/GATE", status="paused_for_human")
    gate["source"]["task_id"] = "GATE"
    store.write_task(lay, "paused", gate)
    passed_verify(lay, "PLAN-001/PH-1/T-1")
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "GATE" not in md.split("Work delivered")[1]


def test_brief_flags_failed_task(lay):
    t = make_task("PLAN-001/PH-1/T-1", status="failed")
    t["failure"] = {"reason": "verification failed: race remains"}
    store.write_task(lay, "failed", t)
    md = brief.build_brief(lay, PLAN, "PH-1")
    assert "Needs your attention" in md
    assert "race remains" in md
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_brief.py -v`
Expected: FAIL — `sb.brief` missing.

- [ ] **Step 3: Implement `sb/brief.py`**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_brief.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/brief.py tests/test_brief.py
git commit -m "feat(sb): phase review brief with rich AgDR profile (pending-review surfaced)"
```

---

### Task 5: `sb/stamp.py` — gate verdict + GATE-task completion

**Files:**
- Create: `sb/stamp.py`
- Test: `tests/test_stamp.py`

- [ ] **Step 1: Write the failing test**

`tests/test_stamp.py`:
```python
import pytest

from sb import claims, seed, stamp, store
from tests.helpers import make_agdr, put_decision

PLAN = {
    "schema_version": "0.1.0", "plan_id": "PLAN-001", "goal": "toy goal",
    "created": "2026-06-14T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [{"task_id": "T-1", "title": "Choose the design",
                    "done": {"statement": "ADR exists"}}]},
        {"phase_id": "PH-2", "name": "Build", "default_model": "haiku",
         "tasks": [{"task_id": "T-2", "title": "Implement it",
                    "depends_on": ["T-1"],
                    "done": {"statement": "tests green"}}]},
    ],
}


def seed_and_finish_ph1(lay):
    """Seed the 2-phase plan and drive PH-1's T-1 to done."""
    seed.seed(lay, PLAN)
    store.move_task(lay, "queued", "done", "PLAN-001/PH-1/T-1")
    _, t = store.find_task(lay, "PLAN-001/PH-1/T-1")
    t["status"] = "done"
    store.write_task(lay, "done", t)


def test_approve_completes_gate_and_unblocks_next_phase(lay):
    seed_and_finish_ph1(lay)
    # PH-2/T-2 is blocked behind the un-stamped gate
    assert claims.claim_one(lay, "w1") is None

    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve",
                      note="LGTM", reviewer="colin")
    assert out["gate_advanced"] is True
    lane, gate = store.find_task(lay, "PLAN-001/PH-1/GATE")
    assert lane == "done" and gate["status"] == "done"

    # next phase is now claimable
    got = claims.claim_one(lay, "w1")
    assert got["id"] == "PLAN-001/PH-2/T-2"


def test_approve_writes_hdr_with_provenance(lay):
    seed_and_finish_ph1(lay)
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="ok")
    _, hdr = store.find_task is None or (None, None)  # placeholder, see below
    rec = store.read_json(
        f"{lay.decisions}/{out['hdr']}.json")
    assert rec["type"] == "human" and rec["id"].startswith("HDR-")
    assert rec["provenance"] == {"plan_id": "PLAN-001", "phase_id": "PH-1"}
    assert rec["status"] == "approved"


def test_approve_stamps_phase_agdrs(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review",
                                plan_id="PLAN-001", phase_id="PH-1"))
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="good call")
    assert out["touched"] == ["ADR-051"]
    rec = store.read_json(f"{lay.decisions}/ADR-051.json")
    assert rec["status"] == "feedback-incorporated"   # note present
    assert rec["feedback"][-1]["action"] == "approve"
    assert rec["feedback"][-1]["note"] == "good call"


def test_stamp_targets_one_decision(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    put_decision(lay, make_agdr("ADR-052", status="pending-review"))
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve",
                      note="", target="ADR-051")
    assert out["touched"] == ["ADR-051"]
    assert store.read_json(f"{lay.decisions}/ADR-052.json")["status"] == "pending-review"


def test_approve_without_note_marks_approved(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="")
    assert store.read_json(f"{lay.decisions}/ADR-051.json")["status"] == "approved"


def test_revise_returns_decisions_to_proposed_and_keeps_gate_paused(lay):
    seed_and_finish_ph1(lay)
    put_decision(lay, make_agdr("ADR-051", status="pending-review"))
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="revise",
                      note="reconsider memory budget")
    assert out["gate_advanced"] is False
    assert store.find_task(lay, "PLAN-001/PH-1/GATE")[0] == "paused"
    assert store.read_json(f"{lay.decisions}/ADR-051.json")["status"] == "proposed"


def test_approve_not_ready_raises_unless_forced(lay):
    seed.seed(lay, PLAN)   # T-1 still queued; gate not ready
    with pytest.raises(stamp.GateNotReady):
        stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="x")
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="x",
                      force=True)
    assert out["gate_advanced"] is True
```

Note: delete the placeholder line `_, hdr = store.find_task is None or (None, None)` — it was an editing slip. The real assertion reads the HDR file directly. Replace that test body with:
```python
def test_approve_writes_hdr_with_provenance(lay):
    seed_and_finish_ph1(lay)
    out = stamp.stamp(lay, "PLAN-001", "PH-1", action="approve", note="ok")
    rec = store.read_json(f"{lay.decisions}/{out['hdr']}.json")
    assert rec["type"] == "human" and rec["id"].startswith("HDR-")
    assert rec["provenance"] == {"plan_id": "PLAN-001", "phase_id": "PH-1"}
    assert rec["status"] == "approved"
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_stamp.py -v`
Expected: FAIL — `sb.stamp` missing.

- [ ] **Step 3: Implement `sb/stamp.py`**

```python
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
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_stamp.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add sb/stamp.py tests/test_stamp.py
git commit -m "feat(sb): gate stamp — records verdict, completes GATE, unblocks next phase"
```

---

### Task 6: CLI wiring — brief / stamp / status / notify

**Files:**
- Modify: `sb/cli.py`
- Create: `hooks/sb_notify.py`
- Test: `tests/test_cli_operator.py`

- [ ] **Step 1: Write the failing test**

`tests/test_cli_operator.py`:
```python
import json
import os

from sb import cli, store
from sb.paths import Layout
from tests.helpers import make_agdr

PLAN = {
    "schema_version": "0.1.0", "plan_id": "PLAN-001", "goal": "toy goal",
    "created": "2026-06-14T00:00:00+00:00",
    "author": {"kind": "model", "id": "claude-fable-5"},
    "phases": [
        {"phase_id": "PH-1", "name": "Design", "default_model": "opus",
         "gate": {"type": "human", "condition": "design ADR approved"},
         "tasks": [{"task_id": "T-1", "title": "Choose the design",
                    "done": {"statement": "ADR exists"}}]},
        {"phase_id": "PH-2", "name": "Build", "default_model": "haiku",
         "tasks": [{"task_id": "T-2", "title": "Implement it",
                    "depends_on": ["T-1"],
                    "done": {"statement": "tests green"}}]},
    ],
}


def run_json(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr().out.strip()
    return code, (json.loads(out) if out else None)


def setup_plan(repo):
    cli.main(["init", "--repo", repo])
    plan_path = os.path.join(repo, "plans", "PLAN-001.json")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(PLAN, f)
    cli.main(["seed", "--repo", repo, "--plan", plan_path])


def test_status_emit_persists_digest(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    capsys.readouterr()
    code, dg = run_json(capsys, "status", "--repo", repo, "--emit")
    assert code == 0 and dg["schema_version"] == "0.1.0"
    assert os.path.exists(os.path.join(repo, ".switchboard", "digest.json"))


def test_brief_prints_markdown(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    lay = Layout(repo)
    with open(os.path.join(lay.decisions, "ADR-051.json"), "w",
              encoding="utf-8") as f:
        json.dump(make_agdr("ADR-051", status="pending-review"), f)
    capsys.readouterr()
    code = cli.main(["brief", "--repo", repo, "--plan", "PLAN-001",
                     "--phase", "PH-1"])
    md = capsys.readouterr().out
    assert code == 0
    assert "# Review: PLAN-001 / PH-1" in md
    assert "ADR-051" in md


def test_stamp_approve_unblocks_next_phase_via_cli(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    lay = Layout(repo)
    # drive PH-1/T-1 to done
    store.move_task(lay, "queued", "done", "PLAN-001/PH-1/T-1")
    _, t = store.find_task(lay, "PLAN-001/PH-1/T-1")
    t["status"] = "done"
    store.write_task(lay, "done", t)
    capsys.readouterr()

    code, out = run_json(capsys, "stamp", "--repo", repo, "--plan", "PLAN-001",
                         "--phase", "PH-1", "--action", "approve", "--note", "ok")
    assert code == 0 and out["gate_advanced"] is True

    code, task = run_json(capsys, "claim", "--repo", repo, "--worker-id", "w1")
    assert code == 0 and task["id"] == "PLAN-001/PH-2/T-2"


def test_stamp_not_ready_exits_2(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)              # T-1 still queued
    capsys.readouterr()
    code = cli.main(["stamp", "--repo", repo, "--plan", "PLAN-001",
                     "--phase", "PH-1", "--action", "approve", "--note", "x"])
    assert code == 2


def test_notify_fires_then_quiet_via_cli(tmp_path, capsys):
    repo = str(tmp_path)
    setup_plan(repo)
    lay = Layout(repo)
    with open(os.path.join(lay.decisions, "ADR-051.json"), "w",
              encoding="utf-8") as f:
        json.dump(make_agdr("ADR-051", status="pending-review"), f)
    capsys.readouterr()
    code, out = run_json(capsys, "notify", "--repo", repo, "--channel", "null")
    assert code == 0 and "pending_agdr:ADR-051" in out["fired"]
    code, out2 = run_json(capsys, "notify", "--repo", repo, "--channel", "null")
    assert out2["fired"] == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_cli_operator.py -v`
Expected: FAIL — unknown subcommands `status`/`brief`/`stamp`/`notify`.

- [ ] **Step 3: Modify `sb/cli.py`**

3a. Extend the imports line. It currently reads:
```python
from sb import claims, paths, query, results, seed, spawn, store
```
Change it to:
```python
from sb import (brief as brief_mod, channels, claims, digest as digest_mod,
                notify as notify_mod, paths, query, results, seed, spawn,
                stamp as stamp_mod, store)
```

3b. Register the new subparsers. After the existing `heartbeat` subparser block (the lines ending with `p.add_argument("--worker-id", required=True)` for heartbeat) and before `a = ap.parse_args(argv)`, insert:
```python
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
```

3c. Add the command handlers. After the existing `heartbeat` handler block:
```python
    if a.cmd == "heartbeat":
        claims.heartbeat(lay, a.worker_id)
        _out({"heartbeat": a.worker_id})
        return 0
```
insert:
```python
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
```

3d. `sb/cli.py` must `import os` (it currently imports `argparse, json, sys`). At the top, add `import os` so the handlers above work. The import block becomes:
```python
import argparse
import json
import os
import sys
```

- [ ] **Step 4: Create the hook shim `hooks/sb_notify.py`**

```python
#!/usr/bin/env python3
"""Thin Claude Code hook shim: runs `sb notify` against the repo so the
worker session (or any hook event) pushes new gate/pause/AgDR/stall signals
through the configured channel. Wired as an actual Claude Code hook in Plan 3;
standalone-runnable now. Edge-triggered, so safe to call on every event."""

import sys

from sb.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["notify", *sys.argv[1:]]))
```

- [ ] **Step 5: Run the operator CLI tests, then the whole suite**

Run: `.venv/bin/pytest tests/test_cli_operator.py -v && .venv/bin/pytest -q`
Expected: 5 passed; full suite green (Plan 1's 84 tests + this plan's new tests).

- [ ] **Step 6: Commit**

```bash
git add sb/cli.py hooks/sb_notify.py tests/test_cli_operator.py
git commit -m "feat(sb): CLI wiring for brief/stamp/status/notify + hook shim"
```

---

### Task 7: Retire gate.py and clean up demo artifacts

**Files:**
- Delete: `gate.py` (replaced by `sb brief` + `sb stamp`)
- Delete: `examples/` (v1 demo brief + task json)
- Delete: `DECISIONS.md` (v1 narrative; real records live in `decisions/`)
- Modify: `README.md`, `CLAUDE.md`

`rabbit_guard.py` stays — Plan 3 rewrites it as deterministic hooks. Delete only what this plan has actually replaced. (`.decisions/` does not exist in this repo, despite the CLAUDE.md backlog mentioning it — nothing to remove there.)

- [ ] **Step 1: Delete the superseded files**

```bash
git rm gate.py
git rm -r examples
git rm DECISIONS.md
```

- [ ] **Step 2: Update README quickstart**

In `README.md`, find the quickstart command block and add the operator surfaces after the claim line. Append these lines to that block:
```bash
sb status --repo . --emit                   # JSON digest -> .switchboard/digest.json
sb brief  --repo . --plan PLAN-031 --phase PH-1   # markdown review brief (PR body)
sb stamp  --repo . --plan PLAN-031 --phase PH-1 --action approve --note "LGTM"
sb notify --repo . --channel macos          # fire new gate/pause/AgDR/stall signals
```
And add one sentence under the surfaces description: "`sb stamp --action approve` completes the phase GATE task, which unblocks the next phase; `sb brief` and `sb status` are the read side (PR body + the notification/nexus digest)."

- [ ] **Step 3: Update CLAUDE.md state**

In `CLAUDE.md`, under "## M0 remaining work", remove the entire **Plan 2 — operator surfaces (next, unwritten):** block (it is now built) and update the "## State" date line and the line `gate.py, rabbit_guard.py are v1 leftovers` to read:
```
- `rabbit_guard.py` is a v1 leftover — do NOT wire to the new layout; Plan 3 replaces it (gate.py was replaced by `sb brief`/`sb stamp` in Plan 2)
```
Add to the "Hard invariants" list:
```
; stamp completes the phase GATE (paused→done) which is the only thing that unblocks the next phase; the digest carries pending-review AgDRs (HDR-010 tier-2 channel)
```
Add a one-line engine-surface update so the surface list includes the new verbs:
```
- Engine surface (Plan 1+2): `sb init|seed|claim|file-result|spawn|requeue-stale|query|heartbeat|status|brief|stamp|notify`; exit codes 0 ok / 2 held / 3 nothing-to-claim
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all green (no test imported `gate` or `examples`).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: retire gate.py (-> sb brief/stamp), drop v1 demo artifacts, update docs"
```

---

## Self-review results

- **Backlog coverage (CLAUDE.md Plan 2):**
  - `sb brief` (phase review brief from results + AgDRs) → Task 4 ✓
  - `sb stamp` (records feedback, completes phase GATE → unblocks next phase, PR-merge oriented) → Task 5 ✓ (`test_approve_completes_gate_and_unblocks_next_phase`)
  - `sb status --emit` (digest: lanes, stale heartbeats, quota state — future nexus read-side) → Tasks 1–2, 6 ✓
  - notify hook (gate ready / paused_for_human / fleet stalled; channel pluggable, macOS default) → Task 3, 6 ✓
  - HDR-010 requirement (pending-review AgDRs route through digest/notification) → digest `pending_agdrs` (Task 2), notify `pending_agdr` events (Task 3), brief "Needs your attention" (Task 4) ✓
  - Demo-artifact cleanup (`.decisions/`, `examples/`, `DECISIONS.md`) → Task 7 ✓ (`.decisions/` absent; noted)
  - Carry-over: malformed verifier verdicts surfaced → digest `stale_active` (Task 2), brief failed-task section (Task 4) ✓
- **Spec coverage:** §5.2 PR-as-gate / brief is the review profile / feedback via `sb stamp` ✓ (Tasks 4–5); §7 `sb status --emit` digest as notify source + nexus read-side, notify on PR/gate-ready / paused / stalled / quota ✓ (Tasks 1–3, 6); §4.2 engine stays git-free, decisions tracked ✓ (stamp does no git); §9 inventory `gate.py → sb brief/sb stamp` ✓ (Task 7).
- **HDR-010 boundary honored:** Plan 2 builds the *channel* (digest carries + notify fires + brief surfaces pending-review AgDRs). The *independent tier judge* is explicitly deferred to Plan 3 and called out in the header and the HDR-010 boundary note — no self-assessment logic is introduced here.
- **Placeholder scan:** one deliberate editing-slip line is flagged in Task 5 Step 1 with its exact corrected replacement; everything else carries complete code or exact edits.
- **Type consistency:** `build_digest(lay, cfg, now=None)`, `build_brief(lay, plan, phase_id)`, `stamp(lay, plan_id, phase_id, action, ...)`, `notify(lay, cfg, dg=None, channel=None)`, `collect_events(dg, seen)`, `channels.resolve(name)` are referenced identically in their tests and in `cli.py`. `gate_ready`/`tasks_in_phase` are shared between `brief.py` and `stamp.py` (stamp imports from brief — single definition, no drift). Digest dict keys match the schema's `required` list exactly. The GATE-completion write-before-move mirrors the invariant already pinned in `sb/results.py`.

## Notes for the executor

- **Run order matters for the suite count.** Plan 1 left 84 tests green. After each task, the suite total grows; `pytest -q` should never show a regression in the Plan 1 tests — if one breaks, you changed a shared module (`validate.NAMES`, `cli.py` imports) incorrectly. The only Plan 1 file this plan modifies is `sb/validate.py` (one additive line) and `sb/cli.py` (additive subcommands + `import os`); neither should alter existing behavior.
- **`sb stamp` is engine-pure by design.** If you feel the urge to `git commit`/`git push` from stamp (as v1 `gate.py` did), don't — spec §4.2 keeps orchestration off branches, and the human performs the PR merge. Stamp's job ends at "feedback recorded, GATE completed, next phase claimable."
- **macOS channel is unverifiable in CI/headless.** All notify tests inject a `collect`/`null` channel; the `macos` channel is best-effort (`check=False`, `capture_output=True`) and never raises, so a missing `osascript` degrades to a no-op rather than failing the loop.
- **`quota.json` is an advisory seam, not a control signal (HDR-011).** This plan only *reads* it (defaulting to `ok`). Two constraints are load-bearing and belong to Plan 3, not here: (1) the *producer* is a deterministic `PostToolUse` hook that detects rate-limit signals token-free — never the throttled session's own reasoning, which may be unable to run under a hard cap; (2) liveness + quota are surfaced to the human by an **external, token-free monitor** (a cron'd `sb status --emit` / `sb notify` that reads `.switchboard/` and makes no model calls), so the signal survives the whole fleet being capped or dead. The engine must never gate `sb claim` on `quota.json` — a shared throttle is simultaneous across the fleet (HDR-009), so a quota-gated claim plus a stale file would wedge everything. There is no Anthropic API for subscription 5h/weekly usage (verified 2026-06-15); the only token-free signals are the reactive 429-on-next-dispatch and, optionally, Claude Code OTEL token counters cross-referenced against published limits.
