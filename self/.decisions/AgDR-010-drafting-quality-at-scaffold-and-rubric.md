# AgDR-010: Encode #14's failure classes at scaffold + rubric, enforce base↔composed parity mechanically

- **Status:** proposed by the issue-#44 implementation session (2026-07-06);
  awaiting ratification at the PR merge gate.
- **Context:** Issue #14 took four triage rounds to reach dispatch; eight of nine
  findings collapse into recurring, checklist-able failure classes (claim-vs-code
  drift, unenumerated consumers of mutated state, fake fidelity per OBS-023, and
  acceptance criteria that name commands outside the worker allowlist). The triage
  verifier was rediscovering these one round at a time. The methodology already
  had a canonical local proof that prose-only coordination fails without
  mechanical enforcement (the #23/#24 merge collision). So the question was not
  *what* to write but *at what altitude* to bind it.
- **Decision:**
  1. Encode the classes in the two executable surfaces every author passes
     through — the `new-ticket.sh --scaffold` skeleton (a new, always-emitted
     `## Consumers of mutated state` section + a citation rule under Assumptions)
     and the `status:triage` rubric (four named reject criteria) — plus one prose
     section in `methodology/METHODOLOGY.md` that the rubric references, so
     drafting and triage share one vocabulary.
  2. Make base↔composed drift **structurally unmergeable**: a conformance test
     (`test_base_and_composed_workflow_are_in_sync`) re-runs register-project.sh's
     placeholder substitution in-process on `WORKFLOW.base.md` and asserts the
     tracked composed `projects/switchboard-self/WORKFLOW.md` matches byte-for-byte.
     Any edit to one file without its mirror is now a red suite — no human memory
     or script run required. The agent edits both files by hand because
     `register-project.sh` is outside the worker allowlist.
- **Rejected (steelmanned):**
  - *A discretionary drafting skill / dynamic principle-puller.* Steelman: richest
    author guidance, adapts per ticket. Rejected: a skill is unenforced whether or
    not it is invoked; the scaffold is the reaching mechanism (oracle-reviewed
    2026-07-06). Prose binds only readers — the #23/#24 collision is the local
    proof.
  - *Prose in METHODOLOGY.md alone.* Steelman: one place, no test surface. Rejected:
    same enforcement-altitude failure; the verifier would keep rediscovering.
  - *A pre-triage gate or new workflow state to force the checklist.* Rejected by
    the ticket's non-goals: this upgrades existing surfaces only; no new states or
    gates.
  - *Sourcing all four substitution values by hardcoding in the conformance test.*
    Rejected: `SB_WORKSPACE_ROOT`/`SB_GITHUB_REPO`/`SB_CONVENTION_ROOT` live in the
    tracked `project.env` (regenerated together with the composed file), so the
    test reads them there and auto-follows a re-registration. Only `{{MAX_AGENTS}}`
    is read back from the composed scalar (register-project.sh does not persist it).
- **Blast radius:** methodology semantics + the triage prompt template (both the
  base source of truth and the composed dogfood copy). Every future ticket author
  and every triage verdict now sees the four classes. The conformance test adds a
  hard coupling: WORKFLOW.base.md and the composed file must always move together.
- **Weakest point:** the conformance test reads `{{MAX_AGENTS}}` back from the
  composed file, so a drift that *only* changes that one integer in the composed
  file (and nothing in base) would not be caught. That is the single placeholder
  register-project.sh does not persist elsewhere; the body-text drift this test
  exists to catch — the rubric/methodology prose — is fully covered because those
  are literals in both files, not placeholders.
