# AgDR-029: Layer a two-field plain-language block above the citation-dense body

- **Status:** proposed by the plain-language implementation session (2026-08-08);
  awaiting ratification at the PR merge gate.
- **Context:** Agent-written tickets, PRs, and triage verdicts are unreadable at
  a glance. This is produced by rules the repo enforces, not by carelessness:
  the drafting-quality checklist requires `file:line` at a named sha for every
  cited mechanism, and the enumeration of every consumer of mutated state. That
  precision is what stops a dispatched session burning turns on a fictional
  claim, so removing it is not on the table. But it means a human catching up —
  or an outside reviewer — reads mechanism before meaning and cannot tell what a
  change actually is or what deserves argument. The question was at what
  altitude to bind a readability layer, and how to keep it from decaying into
  ceremony.
- **Decision:**
  1. Add a fixed `## In brief` block above the existing body on all three
     agent-written surfaces. Two fields only: **What this does** and **What
     could be wrong**. Nothing existing is removed or reworded.
  2. Make both fields hard to pad by constraint rather than by exhortation. The
     first bans identifiers outright — an author who cannot clear it has not
     understood its own change. The second requires an *if X, then Y* shape, so
     a hedge ("coverage could be broader") is structurally rejectable.
  3. Bind it in the executable surfaces every author passes through — the
     `--scaffold` skeleton and the workflow prompt — not in prose alone, and pin
     the strings with tests. Prose binds only readers; this repo's own #23/#24
     collision is the local proof.
  4. Enforce asymmetrically: gate PR bodies at Gate C, template-only for tickets
     and triage verdicts.
- **Rejected (steelmanned):**
  - *Rewrite the templates so plain language is the primary voice, demoting
    citations to an appendix.* Steelman: the cleanest result, and it removes the
    two-audience compromise entirely. Rejected: it fights the triage rubric and
    the drafting checklist head-on, both of which would need reworking, and it
    risks stripping agents of precision they demonstrably use.
  - *Generate a plain-language digest as a bot comment, leaving the sources
    untouched.* Steelman: zero disruption to existing gates. Rejected: a second
    artifact that drifts from its source, and it does not help the reader
    already staring at the PR body — which is the actual failure moment.
  - *Gate tickets on the block at triage too.* Steelman: consistent, maximum
    teeth, and consistent with the view that multi-round triage revisions are
    the point. Rejected: a triage round costs a full dispatched session, and a
    bounce for prose quality is the one bounce that reduces no implementation
    risk.
  - *Add a third field, "where to look first."* Steelman: it would steer the
    review, not just orient it. Rejected to hold the block at two fields; the
    bet is that "what could be wrong" implicitly names the file worth reading.
    This is the field to add back first if drafts read as thin.
  - *Adding the rule to the drafting-quality checklist as a fifth entry* (as the
    design spec proposed). Rejected during implementation: every entry in that
    list is a triage reject criterion the rubric bounces on by name, so filing a
    writing rule there would make triage bounce tickets on prose — which this
    same decision's enforcement asymmetry exists to prevent. It got its own
    section instead.
- **Blast radius:** `scripts/new-ticket.sh` (skeleton), `workflow/WORKFLOW.base.md`
  + `projects/switchboard-self/WORKFLOW.md` (prompt, both halves of the sync
  pair), `methodology/METHODOLOGY.md` (Gate C + a new section), and three test
  files' worth of pins. No `orchestrator/src/` change; no new state, label, or
  gate. The two canary projects declare their own workflow templates and are
  unaffected.
- **Weakest point:** Gate C enforcement is the reviewer reading it, and the
  scarce resource this protects — reviewer attention — is also its enforcement
  mechanism. A reader skimming is the least likely to catch a padded **What
  could be wrong**. Accepted rather than solved: the identifier ban on the first
  field is self-enforcing and does most of the practical work. If padding turns
  out to be common, the follow-up is a mechanical merge-gate check that greps
  the first field for banned tokens (issue numbers, paths, `AgDR-`, `status:`).
  Building that checker now is speculative — the evidence for whether it is
  needed does not exist yet.
