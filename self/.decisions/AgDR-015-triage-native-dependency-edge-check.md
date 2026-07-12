# AgDR-015: Triage rubric checks native dependency edges via `blockedBy`, not `trackedIssues`

- **Status:** proposed by the issue-#40 implementation session (2026-07-12);
  awaiting ratification at the PR merge gate.
- **Context:** In its #31 triage review, a verifier session concluded a stated
  hard dependency "lives only in prose" after querying GitHub's
  `trackedIssues`/`trackedInIssues` — the **task-list hierarchy** — and finding no
  edge. But the scheduler never reads task-lists; it gates dispatch on the
  **`blockedBy`** issue-dependencies GraphQL connection
  (`orchestrator/src/orchestrator/tracker.py:13-14`, re-verified at this HEAD).
  Reading the wrong relationship false-negatives: it reports "no native edge" when
  one exists (drafting-time live example: `#16 ← #15`, `#12 ← #16` present via
  `blockedBy`, invisible to `trackedIssues`). Framing correction (2026-07-07
  verdict): the triage branch of `workflow/WORKFLOW.base.md` carried **no**
  edge-detection instruction — the #31 error was ad-hoc agent reasoning, not a
  documented wrong instruction. `git grep trackedIssues` over `workflow/` and
  `methodology/` returns zero hits, so the fix is verifier-local: add the missing
  guidance to the triage prompt only.
- **Decision:** Add rubric check #10 ("Native dependency edges") to the
  `status:triage` branch of `workflow/WORKFLOW.base.md` (mirrored byte-for-byte
  into the composed `projects/switchboard-self/WORKFLOW.md` per the AgDR-014
  conformance test): when a ticket states a hard dependency, verify it is natively
  chained by querying the `blockedBy` connection
  (`gh api repos/OWNER/REPO/issues/N/dependencies/blocked_by`) and explicitly
  **NOT** `trackedIssues`/`trackedInIssues`. The exclusion is named so a future
  verifier cannot repeat the #31 mistake.
- **Rejected (steelmanned):**
  - *Fix it in `methodology/METHODOLOGY.md` prose instead.* Steelman: one
    canonical home, no drift coupling. Rejected: METHODOLOGY.md already states
    dependencies use native blocked-by (`:22`) and carries no misuse; the gap is
    the verifier's operational instruction, which lives in the prompt template that
    reaches every triage session. Prose binds readers, not the verifier's actions.
  - *Change `new-ticket.sh`'s dependency handling.* Rejected by the ticket's
    non-goals: its *write* path already uses the correct `dependencies/blocked_by`
    endpoint. Only the verifier's *read/audit* guidance was missing.
  - *Build a graph-review analyzer to detect prose-only dependencies.* Rejected:
    that is #37 (closed), which already carries the same `blockedBy`-not-
    `trackedIssues` requirement for that tool. This ticket is prompt-text only.
- **Blast radius:** the triage prompt template (base source of truth + composed
  dogfood copy). Every future triage verdict over a ticket with a stated
  dependency now checks the connection the scheduler actually gates on.
- **Weakest point:** the guidance is void if a future change moves the scheduler
  off `blockedBy` to task-lists (the ticket's stated assumption). The rubric cites
  `tracker.py:13-14` so a drift there is discoverable, but nothing mechanically
  couples the prompt text to that line — a scheduler migration would silently
  invert this instruction until someone re-reads the rubric.
