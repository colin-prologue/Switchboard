# L-10 — Isolation before concurrency; fatal flaws mean redesign

**Incident (Switchboard v1, June 2026).** The first Switchboard was a Python
harness whose design contained an architectural contradiction: it used git
itself as a locking mechanism while gitignoring the state it locked, and ran
concurrent work in a shared working tree. Sessions overlapping in one
checkout had git state mutate under their feet with no errors — silent
corruption, not crashes. It also self-graded its own verification. These were
fatal, not fixable: v1 was archived wholesale and v2 redesigned from
different foundations, rather than patched.

**Lesson 1 — isolation is provisioned, not hoped for.** Every concurrent
session gets a dedicated workspace (fresh clone or worktree, own branch) *by
construction, before* concurrency is granted. Shared-tree safety that depends
on writes happening not to overlap is not safety. Boundary: genuinely
concurrent-safe surfaces (append-only logs, daemon-mediated writes) are
exempt — but that's a property you design and verify, not assume.

**Lesson 2 — some defects demand redesign, not iteration.** Architectural
contradictions and fundamental safety violations discovered late are signals
to re-derive the architecture. Patching them buys you a system whose core
fights itself. Switchboard v2's clean-room rebuild (see L-01) was faster than
rescuing v1 would have been.

**One-authority corollary.** Within v2's daemon, every piece of scheduling
state is mutated from exactly one place (the single event loop); workers
report outcomes and never touch state. Whole classes of races never existed.
Keep that property whatever substrate you choose (I-12).

**Portable.** All of it. Multi-machine actually helps you: engineers' laptops
are isolated by nature. The board becomes the one shared surface — which is
why the claim discipline (I-13) inherits this lesson.
