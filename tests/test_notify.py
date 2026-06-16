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


def test_macos_channel_degrades_when_osascript_missing(monkeypatch):
    # check=False swallows a non-zero exit but NOT a missing-binary OSError;
    # the channel must still degrade to a no-op (plan invariant: never raise),
    # so a non-macOS worker doesn't crash sb notify and wedge its poll loop.
    from sb import channels

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "osascript")

    monkeypatch.setattr(channels.subprocess, "run", boom)
    assert channels.macos("title", "body — em-dash") is None
