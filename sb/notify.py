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
    for a in dg.get("interrupt_agdrs", []):
        out.append((_key("interrupt_agdr", a["id"]), "interrupt_agdr",
                    "AgDR needs immediate review",
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
