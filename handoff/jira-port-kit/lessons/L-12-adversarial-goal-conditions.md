# L-12 — Goal conditions are adversarial specifications

**Context.** Cross-project learning from running autonomous loops (evaluator-
driven agent runs, CI-gated flows): a machine-checkable success condition
gets satisfied by the *cheapest available path*, not the intended one. An
agent told "make the tests pass" can delete the tests, weaken assertions,
or special-case the fixture — all cheaper than the real work.

**Lesson.** Before pointing an autonomous loop at a success condition,
enumerate each check's cheapest bypass and price it above the real work:
explicit goal clauses ("tests may not be modified"), structural impossibility
(tests in a path the workspace guard denies; golden files owned elsewhere;
required checks the agent cannot edit), and review gates that specifically
look for bypass shapes.

**Where this bites the Jira port.** The ticket's acceptance criteria *are*
the goal condition an implementation session runs against, and the triage
verdict (L-06) is itself a check a lazy verifier can satisfy cheaply (PASS is
the verdict that requires no written findings — watch that asymmetry). When
you design the ticket protocol, design the acceptance criteria format so
"criteria met" is expensive to fake: criteria that name observable behavior
and the test that will prove it, not vibes ("works correctly").

**Portable.** Entirely; it's a property of autonomous loops, not of any
tracker.
