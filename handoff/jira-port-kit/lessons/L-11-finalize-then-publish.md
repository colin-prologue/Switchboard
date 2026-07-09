# L-11 — Finalize, then publish: the write-before-move invariant

**Context.** During Switchboard's M0 planning, one reviewed module was found
writing a task file *after* renaming it into the namespace other lanes
consumed — a window where a concurrent consumer picks up a half-written
artifact, causing duplication across lanes. Auditing the plan for the same
*shape* found it in four more planned-but-unwritten modules. The fix was
codified as an explicit invariant ("write-before-move") carried in every
subsequent implementer prompt — five defects prevented at zero
implementation cost.

**Lesson (two of them).**
1. **Never mutate an artifact after publishing it to a consumable
   namespace.** Finalize completely, then publish atomically (write temp →
   move into place; fill all ticket fields → then transition the status).
   This is the root pattern behind a whole class of concurrency defects.
2. **When you find one instance of a structural defect, grep the design for
   the pattern, not just the instance.** The other four were free to find
   once the shape was named — and naming the invariant in implementer
   prompts prevented the class going forward.

**Jira application.** A ticket becomes "published" the moment it enters a
state daemons dispatch from. Populate everything (description, criteria,
links) *before* the transition into a dispatchable state; a daemon that wins
the claim race a second after the transition sees whatever is there. Same
rule for daemon writes: complete the comment/field payload, then transition.

**Portable.** Entirely.
