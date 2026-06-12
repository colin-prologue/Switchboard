# Review: PLAN-031 / PH-1 — Design the concurrency model

**Goal:** Add a session cache that holds up under concurrent load.
**Why this phase:** Decide HOW the cache stays correct under concurrency before any code is written. This is the only compounding decision in the feature, so it gets the most capable tier and a human sign-off.
**Gate condition:** design ADR exists AND status in {approved, feedback-incorporated}

## Decisions made
- **Cache uses immutable state to eliminate concurrent race conditions**  · high confidence · `pending-review`
  - chose **immutable-snapshots**
  - over: mutable-with-locks
  - why: Correctness under concurrent load is the binding constraint; the 12 MB cost is well within budget and buys a lock-free read path. The deadlock in the mutable variant was structural to lock ordering, not a tuning issue.
  - evidence: tests/test_concurrent_cache_stress.rs, memory_overhead_mb, commit abc123def

## Work delivered
- ✓ Choose concurrency model for the session cache
  - Stress-tested both variants at 10k concurrent users for 3h: immutable had zero race conditions; mutable-with-locks deadlocked at ~3h. Memory delta +12 MB.
  - test: tests/test_concurrent_cache_stress.rs
  - metric: memory_overhead_mb = 12

## Needs your attention
- decision awaiting review: Cache uses immutable state to eliminate concurrent race conditions

**You're approving:** the design and work above, advancing past the Design the concurrency model gate.

_Stamp it:_ `gate.py stamp --plan PLAN-031 --phase PH-1 --action approve|revise|flag --note "..."`
