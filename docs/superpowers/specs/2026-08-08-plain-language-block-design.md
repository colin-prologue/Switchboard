# The "In brief" block — plain-language layer for tickets, PRs, and verdicts

**Date:** 2026-08-08
**Status:** shipped on this branch (`claude/plain-language-prs-tickets-667348`);
see AgDR-029 for the one deliberate deviation from this design.
**Anchors verified at:** `ce5764f558b2a8a39d078a7b7e144075f70db318`

## Intent

Switchboard's agent-written prose is unreadable at a glance. A reader catching up —
the operator between context switches, or an outside reviewer helping with the
board — cannot tell from a ticket or PR what changed, what is unusual about it, or
what deserves scrutiny before it merges. The text is dense with issue numbers,
`file:line` citations, AgDR identifiers, label names, and internal function names,
and it leads with mechanism rather than meaning.

This is not an accident of style. It is produced by rules the repo already
enforces. `methodology/METHODOLOGY.md:157` (failure class 1, claim-vs-code drift)
requires every cited mechanism to carry a `file:line` verified at a named HEAD sha.
Failure class 2 requires enumerating every consumer of mutated state. The triage
rubric at `workflow/WORKFLOW.base.md:99-143` enforces both. That precision is
load-bearing — it is what stops a dispatched session burning turns rediscovering
that a claim is fiction — so the fix is not to remove it.

The fix is to add a short human-facing layer **above** it, carrying insight rather
than information: what this change actually is, and what judgment inside it could
be wrong.

## Non-goals

- Removing, relaxing, or rewriting the drafting-quality checklist, the triage
  rubric, or any existing citation requirement. The dense body stays verbatim.
- Changing orchestrator-generated comments. `scheduler.py:1331` (`_park`) and
  `scheduler.py:1200` (`_refuse_missing_marker`) already lead with a bold
  plain-English sentence and a concrete next action. They are the model, not the
  problem.
- Adding a mechanical linter or grep-based checker for the block. Deliberately
  deferred — see "Weakest point."
- Gating tickets on prose quality at triage. Triage rounds cost a full dispatched
  session; a bounce for writing does not reduce implementation risk.

## The block

A fixed, grep-able heading — matching the repo's existing `## Triage verdict`
convention — placed as the first section of every agent-written ticket body, PR
body, and triage verdict comment. Two exceptions may precede it, and only these,
each for its own reason:

- On a **PR body**, a leading `Closes #N` line. Its **presence** is not
  decoration: the orchestrator's handoff validation resolves the issue link
  through GitHub's `closingIssuesReferences` (`orchestrator/src/orchestrator/tracker.py:122,136`,
  checked via set membership at `orchestrator/src/orchestrator/handoff.py:246`),
  which matches a closing keyword **anywhere** in the PR body — so a missing
  reference breaks the `status:human-review` transition, but its **position**
  does not. Keep it as the first line by convention only, so it stays visible
  and never gets edited away; block second.
- On a **triage verdict comment**, the exact heading `## Triage verdict`, which
  `workflow/WORKFLOW.base.md:154` pins as the comment's first line for
  grep-ability — a human/tooling convention, not a machine check. Block second.

The block itself:

```markdown
## In brief

**What this does:** <one sentence>

**What could be wrong:** <one named decision or assumption, and the concrete
consequence if it is false>
```

Two rules make the fields hard to pad. Both are stated in the templates as
constraints on the author, not aspirations.

### Rule 1 — "What this does" bans identifiers

No issue numbers, no file paths, no AgDR/ADR/OBS/PHI identifiers, no `status:*`
label names, no function, class, or field names.

This single constraint is the mechanism. It is trivially checkable by eye, and an
author who cannot clear it has not understood its own change well enough to
summarize it. It is what buys the twenty-second glance.

### Rule 2 — "What could be wrong" requires a conditional and a consequence

The sentence must have the shape *if X, then Y*: a named assumption or decision,
and what concretely breaks when it does not hold.

Rejectable shapes are those that name a quality instead of a consequence —
"test coverage could be broader," "this could be more robust," "the approach may
not scale." Passing shapes name the trigger and the damage.

This field is the "scrutiny before moving on" surface. It is the one that decays
first if unenforced, which is why it is the one gated at merge.

### Worked example

PR #115 currently opens:

> Closes #61. Records the ownership change as AgDR-028.
>
> ## What landed
>
> **Evidence contract** (`orchestrator/handoff.py`): a worker's FINAL task action
> is writing `.run/handoff-evidence.json` …

Under this design, that body is unchanged but for the block inserted after the
`Closes` line:

> Closes #61. Records the ownership change as AgDR-028.
>
> ## In brief
>
> **What this does:** Agents can no longer mark their own work ready for review.
> They leave a note saying which pull request they opened, and the orchestrator
> checks that note against reality before moving the ticket into your queue.
>
> **What could be wrong:** Changing the ticket's state takes two separate GitHub
> calls with no way to do both at once. If the process dies between them, the
> ticket briefly shows two conflicting states. A read-back catches this while the
> session is alive, but a crash inside that window leaves it wrong until a human
> looks.

## Surfaces and edits

| Surface | File | Change | Enforcement |
|---|---|---|---|
| Ticket bodies | `scripts/new-ticket.sh:43-68` | Block as the first section of the `--scaffold` skeleton, above `## Intent` | Template only |
| PR bodies | `workflow/WORKFLOW.base.md:198` (step 7) | Block as the first section after the `Closes #N` line | **Gate C** |
| Triage verdicts | `workflow/WORKFLOW.base.md:154` (NEEDS WORK routing) | Block as the first section under the `## Triage verdict` heading | Template only |
| Gate C definition | `methodology/METHODOLOGY.md:65` | Add the block as a merge-gate completeness condition | — |
| ~~Drafting guidance~~ | ~~`methodology/METHODOLOGY.md:145`~~ | **Superseded, not implemented.** Every entry in the drafting-quality checklist is a triage reject criterion; filing a writing rule there would make triage bounce tickets on prose. The block got its own `## Writing for the reader` section instead. See AgDR-029's rejected options (the fifth bullet). | — |
| Composed prompt | `projects/switchboard-self/WORKFLOW.md` | Mirror the base edits (see "Drift") | — |

Orchestrator-generated comments are explicitly out of scope; no Python changes.

### Enforcement at Gate C

`methodology/METHODOLOGY.md:65` currently makes an unratified AgDR grounds for
bouncing a PR at the merge gate. The block joins it: a PR whose body lacks the
block, or whose "What could be wrong" names a quality rather than a consequence,
is **incomplete** and bounces the same way.

This is human enforcement by the merging reviewer. It is not automated.

### Drift

`workflow/WORKFLOW.base.md` is a template. `scripts/register-project.sh` composes
per-project `WORKFLOW.md` files from it, and both `scripts/verify-setup.sh:121` and
`orchestrator/tests/test_workflow.py:961` fail when a composed file drifts from its
template.

Only `projects/switchboard-self/WORKFLOW.md` derives from the base — `codex-canary`
and `mixed-canary` declare their own templates via `SB_WORKFLOW_TEMPLATE` in
`project.env`, and are unaffected. **Both `workflow/WORKFLOW.base.md` and
`projects/switchboard-self/WORKFLOW.md` must be edited in the same commit**;
`register-project.sh` is outside the worker allowlist, so recomposition is not
available to a dispatched agent.

## Testing

The scaffold change trips three pinned tests in
`orchestrator/tests/test_new_ticket.py`, each of which must be extended:

- `test_scaffold_emits_all_sections_and_exits_clean` (line 45) — add `## In brief`
  to the asserted section list.
- `test_scaffold_pins_drafting_quality_content` (line 59) — pin both field labels
  (`**What this does:**`, `**What could be wrong:**`) so the block cannot be
  silently gutted.
- `test_scaffold_output_is_valid_dry_run_body` (line 143) — add `## In brief` to
  the round-trip section assertions.

The workflow change trips the drift test in `orchestrator/tests/test_workflow.py`,
which passes once both files are edited together.

Suite command (inside the worker allowlist):

```
uv run --project orchestrator python -m pytest orchestrator/tests -q
```

No new test file is warranted. This is a documentation and template change; the
existing pins are the correct regression surface.

## Weakest point

**Gate C enforcement is the reviewer reading it.** The scarce resource this design
protects — reviewer attention — is also its enforcement mechanism. A reader
skimming is the least likely to notice a padded "What could be wrong."

This is accepted rather than solved. The identifier ban in Rule 1 does most of the
practical work and is self-enforcing (an author either wrote a plain sentence or
did not). If padding turns out to be common in practice, the follow-up is a
mechanical merge-gate check that greps the "What this does" line for banned tokens
— issue numbers, paths, `AgDR-`, `status:`. Building that checker now is
speculative; the evidence for whether it is needed does not exist yet.

**Secondary risk: ceremony.** Three surfaces times two fields is six new fields an
agent fills on every unit of work. If drafts start reading as filler, the first
thing to drop is the block on triage verdicts — those are the shortest-lived
artifact and the one whose reader (the ticket author) already has full context.

**Dropped from the design:** a third field, "where to look first," pointing the
reviewer at the one or two things worth reading. Cut to keep the block at two
fields. The bet is that "What could be wrong" implicitly names the file worth
reading. If drafts orient the reader but fail to steer the review, this is the
field to add back.

## Decisions worth recording

This changes methodology semantics (`methodology/METHODOLOGY.md` Gate C
completeness conditions and the drafting-quality checklist) and a workflow prompt
template. Per `workflow/WORKFLOW.base.md:192`, the implementing PR must carry an
AgDR at `self/.decisions/AgDR-NNN-<slug>.md` or it is incomplete at the merge gate.
