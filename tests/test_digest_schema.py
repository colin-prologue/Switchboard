import json

import pytest
from jsonschema import Draft202012Validator

from sb import validate


def test_digest_registered_in_validate():
    assert validate.NAMES["digest"] == "digest.schema.json"


GOOD_DIGEST = {
    "schema_version": "0.2.0",
    "generated_at": "2026-06-14T00:00:00+00:00",
    "lanes": {"queued": 1, "active": 0, "paused": 2, "done": 3, "failed": 0},
    "gates_ready": [{"id": "PLAN-001/PH-1/GATE", "condition": "phase PR merged"}],
    "paused_for_human": [{"id": "PLAN-001/PH-1/T-9", "reason": "missing credential"}],
    "pending_agdrs": [{
        "id": "ADR-051", "title": "Use immutable snapshots",
        "confidence": "medium", "blast_radius": "cache module only",
        "provenance": {"plan_id": "PLAN-001", "phase_id": "PH-1"},
    }],
    "interrupt_agdrs": [],
    "record_silent_agdrs": [],
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


def test_digest_has_three_agdr_buckets(lay):
    from sb import digest
    dg = digest.build_digest(lay, {})
    for k in ("pending_agdrs", "interrupt_agdrs", "record_silent_agdrs"):
        assert isinstance(dg[k], list)
    assert dg["schema_version"] == "0.2.0"
