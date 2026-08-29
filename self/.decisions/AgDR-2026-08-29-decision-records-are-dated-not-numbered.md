# AgDR-2026-08-29 — Decision records are dated, not numbered

- **Date:** 2026-08-29
- **Issue:** #154 (decision-record numbers collide between parallel branches)
- **Status:** proposed (operator ratifies at Gate C)

## Context

A record's number was minted when a session **started** a ticket and validated
when that ticket **merged**. Nothing reserved it in between, so two branches
opened in the same window read the same `main`, picked the same next-free
number, and the second to merge went red — at the point where the work was
otherwise finished. Three collisions landed on 2026-08-15 alone (`AgDR-038`,
`AgDR-036`, `AgDR-039`). The third is the informative one: its CI was *green*,
because the PR's last run predated the colliding record landing. It was caught
only because a human went looking.

The renames are cheap. The cost is that they arrive mid-merge, when attention is
elsewhere, and each one drags a cross-reference sweep through prose, code
comments, and test docstrings.

`test_decision_record_numbers_are_unique_and_match_headings` caught every
instance. It works. The problem was never detection — it was that detection
could only happen after both branches existed, which is after the number was
already spent.

## Decision

Drop the number. New records are `AgDR-YYYY-MM-DD-<slug>.md`, cited in prose by
slug. The `AgDR-`/`ADR-` prefix is a record *type* marker and is unchanged; only
the number is replaced.

Sequential numbering is a central allocator in a system that has gone
distributed — parallel sessions, separate worktrees, one repo per project. No
reservation discipline fixes a shared counter with concurrent writers, and a
reservation step that is easy to skip will be skipped. Meanwhile the records
were *already* named `AgDR-045-gate-states-are-declared.md`: the slug carries
all the meaning and the number carries none. The number was simultaneously the
only part that could collide and the only part that was uninformative.

Legacy records (`ADR-000`, `AgDR-001`–`AgDR-048`) are frozen in place, not
renumbered. The seam is self-describing: a number means pre-changeover.

Three surfaces move together: the mint instruction in `workflow/WORKFLOW.base.md`
worker rule 7 (plus its composed mirror and the codex pilot variant — that line
is what actually mints records, so a decision that skips it is a dead letter),
the uniqueness test, and a new `self/.decisions/README.md` carrying the citation
convention.

## Rejected

- **Reserve at ticket creation** — `new-ticket.sh` allocates the number into the
  ticket body, moving collisions to filing time, which is serial and
  human-paced. Keeps the central allocator, adds a step, and burns numbers on
  tickets that never produce a record. The ticket's own warning applies: a
  reservation scheme that is easy to skip will be skipped.
- **Assign at merge** — drafts go unnumbered and are numbered when they land.
  Removes the collision, but every cross-reference written *during* development
  points at a name that does not exist yet, which is a worse authoring
  experience than the problem being solved.
- **Derive from the issue number** (`AgDR-i133-slug`) — collision-free by
  construction, and genuinely tempting. But it couples the decision namespace to
  GitHub, has no answer for decisions with no ticket, and needs a suffix scheme
  the first time one issue yields two records.
- **Leave it, improve the diagnostic** — make the test name the free number and
  the references to sweep. Defensible on the observed rate: the three collisions
  came from one unusually parallel day. Rejected because throughput returned
  (`AgDR-045`–`AgDR-048` plus two sweeps between 2026-08-19 and 2026-08-28), and
  because the same problem is live in other repos — worth fixing once rather
  than per-repo.

**What sequential actually bought, audited:** chronological sort (a date prefix
does it better and explicitly — kept); compact prose citation (given up: ~8
chars vs ~20, but the long form needs no lookup); a sense of corpus volume
(given up: `ls | wc -l`); gaps revealing deletions (given up: never used).

## Blast radius

Nothing dispatches on decision records and no orchestrator behaviour reads them
— they are read by humans, by this one test, and by cross-references in prose.
The test now carries both forms with uniqueness enforced per key space (number
for legacy, date+slug casefolded for dated), so no existing record moves and the
first dated record is this one. The mint instruction changes what every future
worker session writes, which is the point; the base↔composed sync test forces
the two prompt files to move together. `scripts/new-ticket.sh` is untouched —
nothing about ticket filing changes.

## Weakest point

**This trades a loud failure for a silent one.** Two sessions that decide the
same thing on different days under different slugs will never collide, so
nothing goes red — where the old scheme would at least have gone red on the
number and forced a conversation. The check stops reporting bookkeeping
accidents, which were noisy and harmless, and has no equivalent grip on
substantive duplicates, which are quiet and harmful.

This is accepted, not solved. The mitigation is a periodic dedupe read of the
directory — the `SWEEP-` files are that habit already — and not a naming scheme,
because no filename convention can tell that two differently-worded slugs name
the same decision.

The prediction to re-read: *if a duplicate decision reaches merge unnoticed and
the sweep habit did not catch it within a few weeks, the mitigation is too weak
and the answer is a scheduled dedupe pass, not a return to numbering.*
