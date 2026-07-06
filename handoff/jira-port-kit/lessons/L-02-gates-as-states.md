# L-02 — Gates cost zero orchestrator code when they are states

**Context.** Switchboard's human checkpoints (draft review, plan review,
PR review, parked) required no gate logic in the scheduler at all. The
scheduler dispatches only from an *active set* of ticket states; every gate
is simply a state outside that set.

**Lesson.** Model methodology as ticket state, not as code. The scheduler
stays policy-agnostic (it knows "active states", nothing else); the
methodology lives in configuration (which states exist, which are active,
what the prompt tells the agent to do in each). Changing the workflow —
adding a gate, removing one — is a config change, not a scheduler change.
This is what made the methodology iterable at all.

**Corollary.** Role behavior lives in the *prompt*, selected by state (the
triage verifier and the implementer ran on identical dispatch machinery with
a role-swapped prompt — see L-06). The scheduler never knows roles exist.

**Portable.** Entirely. Jira makes it more natural: statuses are first-class,
and if you get workflow control you can enforce transitions server-side
(conditions/validators) — an enforcement layer GitHub labels never had
(I-1 upgrade opportunity).

**Watch out.** The scheduler being policy-blind means a prompt bug (verifier
that implements) ships uncaught — the role boundary is prose. Switchboard
accepted this; you have I-7 (state change kills session) as the backstop.
