# Decision records

Why Switchboard is built the way it is. Three kinds of file live here, and the
prefix says who wrote it:

| Prefix | Author | Ratified by |
|---|---|---|
| `ADR-` | a human | itself |
| `AgDR-` | an agent session, in the PR that made the call | the operator, out of band |
| `SWEEP-` | a re-reading of the records, not a new decision | n/a |

## Naming

New records are **dated, not numbered**:

```
AgDR-YYYY-MM-DD-<slug>.md
```

The date is the day the record was written. The slug is a short kebab-case
phrase naming the decision — a claim, not a topic (`gate-states-are-declared`,
not `gate-states`). The H1 heading inside repeats the filename stem:

```markdown
# AgDR-2026-08-29-decision-records-are-dated-not-numbered
```

Records numbered `ADR-000` and `AgDR-001` through `AgDR-048` predate this
convention. They are **frozen**: never renumbered, never renamed, never deleted.
A number means pre-changeover, and that is all it means. Both forms are checked
by `test_decision_record_numbers_are_unique_and_match_headings` in
`orchestrator/tests/test_workflow.py`, and the freeze by
`test_legacy_numbered_records_are_not_renamed` beside it.

### Why the number went away

A sequential number is a central allocator, and Switchboard runs distributed —
parallel sessions, separate worktrees, one repo per project. Two branches opened
in the same window both read the same `main`, both picked the same next-free
number, and the second one to merge went red at the worst possible moment.
Three such collisions landed on 2026-08-15 alone.

The number was also the only uninformative part of the name: `AgDR-045` tells
you nothing that `gate-states-are-declared` does not tell you better. Dropping
it removes the only field that could collide. See
`AgDR-2026-08-29-decision-records-are-dated-not-numbered.md` for the rejected
alternatives and the residual risk this accepts.

## Citing a record

**Cite by slug.** In prose, name the decision the way you would say it out loud:

> supersedes the durable-park-label record
> the stance-ladder record already settled this
> per `AgDR-2026-08-29-decision-records-are-dated-not-numbered`

The slug is the stable, searchable, meaningful part — a reader can find the file
with it and can often skip opening the file at all. When you need to be exact
(a supersession, a cross-repo reference), write the full filename stem.

Legacy records keep their numbers as their names, so cite them the way they are
named: `AgDR-039`, or `AgDR-039-per-project-stance-ladder` where the extra
words help. Do not translate an old number into a date.

## What is not solved here

Two sessions can decide the same thing on different days under different slugs.
Nothing collides, so nothing goes red — the old scheme would at least have
gone red on the number. That trade is deliberate: it swaps a loud failure about
bookkeeping for a silent one about substance. The mitigation is a periodic
dedupe read of this directory (the `SWEEP-` files are that habit), not a
naming scheme.
