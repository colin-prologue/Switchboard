# AgDR-045 — A stance declares its gate states; the board check judges against that

- **Date:** 2026-08-15
- **Issue:** #52 (board-state sanity check — detect, never revert)
- **Status:** proposed (operator ratifies at Gate C)

## Context

#52 asks for a stance-independent board check whose first condition is "a
`status:*` label absent from the project's `active_states`, its
`terminal_states`, its `handoff_label`, and the known durable markers".

Applied literally to the shipped `base` template, that set is
`{triage, todo, in progress, closed, human review, parked}`. Every Gate A/B
state `base` actually uses — `drafting`, `plan review`, `decision`, `blocked` —
is absent from it, because **a gate in this system is defined by nothing
dispatching it** (METHODOLOGY.md, "Proportionality"). So the literal reading
reports this very repo's Gate A queue as invalid, on every poll tick. #52's own
acceptance criteria forbid that: "a check that flags one stance's normal
operation as invalid is worse than no check."

The same criteria forbid the obvious patch — "no hard-coded state list" — so
importing `status_board.STATE_TO_OPTION` (which does enumerate every known
state) is out, and `workflow/transitions.yml` is a named non-goal.

## Decision

Add `tracker.gate_states` to the workflow config: the states a stance
**defines but never dispatches**. `base` declares
`["drafting", "plan review", "decision", "blocked", "human review"]`;
`prototype` declares `["human review"]` — the one state its QA role escalates
to. The board check judges an observed label against
`active_states ∪ terminal_states ∪ gate_states ∪ {handoff} ∪ {parked}`, all
config-supplied.

Nothing dispatches off the new field, and the loader does not validate it
against anything. It is a vocabulary declaration, not a control surface.

## Rejected

- **Take the literal definition and accept the false positives.** Honest to the
  ticket, useless in practice: the first thing it would report is four
  legitimate `base` states, and a check whose first output is noise gets muted
  before it ever catches a real bug.
- **Derive the vocabulary from `status_board.STATE_TO_OPTION`.** It is the one
  existing total list of known states, and reusing it adds no new config. But
  it is stance-blind by construction — it must carry every status so
  label→field mirroring is total — so `prototype` would accept `status:triage`,
  a state its recipe cannot produce and nothing there will move. It also makes
  the check answer from the board's needs rather than the project's, which is
  the second-reader failure METHODOLOGY.md's "conflict is evidence" section
  describes.
- **Infer gates from the prompt body.** The composed `WORKFLOW.md` does name
  its states in prose. Parsing prose for a runtime predicate is a drift
  generator, and the failure is silent.
- **Drop condition 1 and ship only conditions 2 and 3.** Cheapest, and it needs
  no new config — but the undefined label is the condition most likely to be
  produced by the bug class #52 exists to catch (a hook or Action writing a
  state nobody defined), and the other two are recoverable by eye.

## Blast radius

`TrackerConfig` gains a field with an empty default, so an unmodified project
config loads unchanged and simply has a narrower defined vocabulary — it
reports more, never fewer. Both shipped templates and the composed
`projects/switchboard-self/WORKFLOW.md` mirror change together (the sync test
enforces it). No dispatch, eligibility, handoff, or role-pin behaviour reads
the field.

## Weakest point

**`gate_states` is a hand-maintained second list of the same states the prompt
body already names, and nothing binds the two.** Add a state to a stance's
prompt without adding it here and the check reports every ticket that reaches
it — the noise-then-mute failure above, arriving later and by a different
route. The mitigation is deliberately weak (a comment in each template saying
what the list is for) because the strong form — validating declared states
against the prompt — means parsing prose, which is the option rejected above.

The prediction to re-read: *if a stance gains or renames a non-dispatched state
and this list is not updated in the same commit, the board check will report
legitimate tickets.* If that fires, the answer is probably to make
`register-project.sh` emit the list from one place rather than to add
validation.
