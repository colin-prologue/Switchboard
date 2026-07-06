# L-03 — Caps are diagnostic checkpoints; parking is the mechanism

**Context.** The upstream orchestration core happily re-dispatched an active
ticket forever. With paid execution, unbounded re-dispatch is unbounded
spend. Switchboard capped sessions per ticket (default 3) and **parked** on
exhaustion: one notification comment, a durable parked marker, claim
released, no re-dispatch until a human deliberately unparked.

**Incident (OBS-022, the self-unpark loop).** The first parking design
unparked on "ticket was updated". The parking *notification comment itself*
bumped the ticket's updated-timestamp — so parking immediately unparked,
re-dispatched, re-parked, re-commented: an unbounded spend loop. The fix was
structural, not a patch: unparking became "human removes the parked marker",
so no system-generated activity can wake a ticket. A second, subtler defect
followed on the error path: the in-memory parked flag was set *before* the
marker write; if the write failed, the next scheduling pass took the unpark
branch and re-dispatched — the same loop, resurrected by a failed write.
Fix: record parked only after the durable write succeeds (I-9 shape).

**Lesson.** A cap that silently kills work destroys the diagnostic. A cap
that pauses with visibility ("tried 3 times, here's the trail, investigate")
turns spend limits into a debugging instrument. And the wake condition must
be a deliberate human act on a durable marker — never derived from activity
that the system itself can generate.

**Portable.** Entirely — caps, parking, deliberate unpark, and both incident
shapes (activity-derived wake conditions; state recorded before its durable
write). With N daemons the durable marker matters even more: memory was
never authoritative (I-4).
