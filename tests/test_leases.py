from sb import leases


def test_lease_roundtrip(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    lease = leases.read_lease(lay, "PLAN-001/PH-1/T-1")
    assert lease["worker_id"] == "worker-a"
    assert lease["ttl_s"] == 100


def test_missing_lease_reads_none(lay):
    assert leases.read_lease(lay, "PLAN-001/PH-1/T-9") is None


def test_expiry(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    lease = leases.read_lease(lay, "PLAN-001/PH-1/T-1")
    assert not leases.is_expired(lease, now=lease["claimed_at"] + 50)
    assert leases.is_expired(lease, now=lease["claimed_at"] + 101)


def test_corrupt_lease_reads_as_stale(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    with open(leases.lease_path(lay, "PLAN-001/PH-1/T-1"), "w", encoding="utf-8") as f:
        f.write("{torn write")
    assert leases.read_lease(lay, "PLAN-001/PH-1/T-1") is None


def test_clear_is_idempotent(lay):
    leases.write_lease(lay, "PLAN-001/PH-1/T-1", "worker-a", ttl_s=100)
    leases.clear_lease(lay, "PLAN-001/PH-1/T-1")
    leases.clear_lease(lay, "PLAN-001/PH-1/T-1")  # no error
    assert leases.read_lease(lay, "PLAN-001/PH-1/T-1") is None
