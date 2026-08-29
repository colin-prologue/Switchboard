# AgDR-049 — The entry state is resolved from the target project, and an auto-resolved `todo` stamps the triage marker

- **Date:** 2026-08-29
- **Issue:** #176 (default stance + default entry state compose into a ticket that is never dispatched)
- **Status:** proposed (operator ratifies at Gate C)

## Context

Two defaults, each defensible alone, composed into a dead ticket.
`register-project.sh` defaults `--stance prototype`; `new-ticket.sh` defaulted
`--entry triage`. `prototype`'s `active_states` is
`["todo", "in progress", "review"]` and its `gate_states` is `["human review"]`,
so a `status:triage` label on a `prototype` project is neither active, nor
terminal, nor a gate. Nothing dispatches it and nothing is waiting for it. The
ticket sits, indistinguishable from one legitimately parked, and the tool that
filed it says nothing.

`new-ticket.sh` was entirely stance-blind: it validated the `--entry` *value*
against `drafting|triage|todo` but never asked what the target project
dispatches. The README carried a warning instead — a memory aid, which this
project has repeatedly found is the class of remedy that fails the same way
(METHODOLOGY.md, "Conflict is evidence"). Detection exists since #161/#168 (the
board check's `undefined-status-label` condition reports exactly this label), but
it fires *after* the dead ticket is filed.

Two decisions were needed, and the second is the sharp one.

## Decision

**1. The entry state is derived from the target project, or refused.**

`new-ticket.sh` resolves the repo to `projects/<slug>/WORKFLOW.md` by matching
`SB_GITHUB_REPO` across `projects/*/project.env`, then reads `active_states` and
`gate_states` from that composed file. With no `--entry`:

- `triage` if the project dispatches it (so `base` is unchanged),
- else `todo` if the project dispatches it,
- else a non-zero refusal naming the states the project *does* dispatch.

Resolution failure (no binding, an ambiguous binding, a missing or
`active_states`-less `WORKFLOW.md`) is also a non-zero refusal naming `--entry`
as the fix. It never falls through to a guess. An explicit `--entry` naming a
state the project neither dispatches nor declares as a gate is refused the same
way — the explicit path must not be silently worse than the defaulted one — and
an explicit `--entry` still works when resolution failed, because that is what
the refusal tells the operator to do.

The state **list** is read, not the stance **name**: `SB_WORKFLOW_STANCE`
post-dates several bindings (`projects/switchboard-self/project.env` has no such
line) and bindings are only rewritten on re-registration, whereas `active_states`
is present in every composed file and is indifferent to how many stances exist.
A mechanism branching on stance names would already be written against an
interface wider than the two templates that exist.

The resolution **mirrors** `status_board.workflow_for_repo()` step for step
rather than inventing a second repo→project map, and a test pins the two
implementations to the same answer for a real binding. It is mirrored rather
than invoked because bash cannot import Python and the worker allowlist admits
no ad-hoc interpreter run; the pinning test is what stops the copy from becoming
a second, disagreeing map (the `AgDR-043` failure).

**2. An auto-resolved `todo` stamps `gate:triage-passed` — the same rule as an
explicit one.**

The issue recommended *not* stamping, on the grounds that the in-code
justification ("the human filing it IS the out-of-band verification") asserts a
human decision that did not happen. That is rejected, for two reasons:

- **The counterargument is decisive under the guard as it exists.**
  `workflow/transitions.yml`'s `requires_marker` is repo-global, not per-project:
  the dispatch guard refuses an unstamped `status:todo` on *every* project. An
  auto-resolved `todo` that did not stamp would be refused at dispatch — exactly
  the never-dispatched ticket this change exists to prevent, moved one step
  later. Not stamping is only coherent alongside a stance-aware guard, and
  reworking the dispatch guard's provenance semantics is a different change with
  a different blast radius.

- **The provenance is not forged; it is granted one level up.** At a stance with
  no triage step, the marker cannot mean "a verifier passed this" — there is no
  verifier. It means the gate this project's stance omits is satisfied. The
  operator made that call when they registered the stance, and the stance is a
  more durable statement of it than a per-ticket flag. The explicit `--entry
  todo` path has stamped on this reasoning since #29; the auto path is the same
  act with the same authority.

The rule is therefore unchanged and unbranched: entry `todo` stamps. What
changed is only *how* the entry state is chosen.

## Rejected alternatives

- **Docs only (keep the warning, fix nothing).** Steelman: it is one line, it
  costs nothing, and the trap is now written down where the quick start puts it.
  Rejected because the warning lives in a block the reader passes once, and the
  tool still files the dead ticket for anyone who does not re-read it. A default
  that needs a footnote is a defect in the default.

- **Change `prototype`'s `active_states` to include `triage`.** Rejected as an
  explicit non-goal of #176, and rightly: the stances are correct: `prototype`
  omitting triage is the whole point of the loose end of the ladder. The
  interaction is what was wrong.

- **Flip `new-ticket.sh`'s fixed default to `todo`.** Steelman: one-line change,
  fixes the `prototype` case immediately, and `todo` is the commoner entry.
  Rejected because it inverts the bug rather than removing it — every `base`
  project would then skip triage by default, which is worse (an unverified
  contract reaching an implementation session), and it still hard-codes a
  constant where the answer is per-project.

- **Refuse without `--entry`, always — the "cheap loud refusal is the whole
  answer" branch #176 offered.** Steelman: no resolution code, no bash/Python
  mirror, no drift risk; every filer states the state explicitly. Rejected
  because the resolution turned out to be ~50 lines of bash over a file the
  install already ships, and because a refusal that fires on the common path
  teaches operators to type `--entry todo` reflexively — including on `base`,
  where that silently skips triage. A refusal is the fallback here, not the
  mechanism.

- **Make the dispatch guard stance-aware and drop the stamp.** Steelman: it is
  the honest fix, and it removes a marker whose name lies about its provenance at
  `prototype`. Rejected as out of scope for a ticket about a filing tool: it
  changes when the orchestrator will claim work, on every project at once. If it
  is taken up, this record's stamp rule is the thing to revisit.

## Blast radius

- **`scripts/new-ticket.sh`** — filing without `--entry` against a repo with no
  Switchboard binding now refuses where it previously filed at `status:triage`.
  This is a deliberate behaviour break: that path is precisely the one that
  produced dead tickets. `--entry` is the documented escape hatch and works
  everywhere.
- **`gate:triage-passed`** on `prototype` projects — now written by the default
  path, where previously it required `--entry todo`. Same label, same consumers
  (dispatch guard, `transitions.yml` marker effects, board sync's untouched
  non-`status:` passthrough); more of them, on projects that never triage.
- **`SB_HOME`** — `new-ticket.sh` now resolves it (`BASH_SOURCE`, env override),
  which it never did. An operator running a copy of the script detached from its
  install gets a resolution failure, not a wrong answer.
- **Docs** — the README quick-start caveat and the `prototype` collapse paragraph
  are deleted: they existed only to compensate for the default that is gone.

## Weakest point

**The mirror.** There are now two implementations of repo→project resolution — 
`status_board.workflow_for_repo()` in Python and ~25 lines of bash — held
together by one test against one real binding. That test proves they agree on
the happy path; it does not prove they agree on the edges (a binding with
`SB_GITHUB_REPO` quoted, a symlinked `projects/` dir, case folding on a
repo name). If the Python side grows a resolution rule the bash side does not
learn, `new-ticket.sh` will file against a state machine the scheduler is not
using — and it will do so *confidently*, printing a resolved workflow path in
`--dry-run`. The failure would look like success.

The second-weakest: this record asserts the stance grants the triage-PASS
provenance. If `prototype` ever gains a cheap pre-dispatch verification step,
that assertion silently becomes false and the marker resumes lying — with the
default path, not just the explicit one, producing it.
