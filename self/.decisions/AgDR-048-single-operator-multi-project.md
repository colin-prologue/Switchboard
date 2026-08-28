# AgDR-048 — Switchboard is a single-operator, multi-project system

- **Status:** proposed (ratify at the adopting PR's merge gate)
- **Issue:** queue triage 2026-08-28 (operator declaration; no single parent ticket)
- **Date:** 2026-08-28
- **Supersedes / amends:** retires the human-drag premise behind #22/#141/#156/#164
  and AgDR-044's arbitration model; converts AgDR-042's named repo-scoped
  residual from *unresolved hazard* to *accepted risk under a stated constraint*.

## Context

The original design left the door open for multiple humans: a Projects board as
the human control surface (drag a card, automation honors or reverts it —
AgDR-044), plural `operator_logins` config, and a cross-runner claiming roadmap
(#15/#56) so several orchestrator processes could coordinate through the
tracker.

Observed reality, June–August 2026: the operator never dragged. Queue control
happened entirely through session-mediated signals — triage rounds, fold-loop
proposals applied headlessly (AgDR-035), `blockedBy` edges, label transitions
performed by sessions. The board sync merged in #141 was **never installed**
(#157 sat staged for two months) and the system ran fine without it — the board
is provably not load-bearing. The one observability pain actually filed in that
window was #174: "what is running right now is invisible" — an observability
complaint, not a control complaint.

On 2026-08-28 the operator made it explicit: this is a one-person studio.
Multiple seats are not a dormant requirement; they are a non-requirement. The
trusted delegation target is agents and sessions, not colleagues.

## Decision

### 1. The control surface is session-mediated signals; the board is retired

Labels, comments, and the fold loop are the API through which the queue is
ordered, activated, and developed — for the operator and for agents alike. The
Projects board is retired as a control surface: #157 is closed and PR #179 is
closed unmerged. The staged workflow file
(`deploy/github-workflows/status-board-sync.yml`) is **retained, not deleted** —
reopening is a `git mv` plus two credentials, and the arbitration logic
(#156/#164) stays correct in the tree.

### 2. One orchestrator process per repo, ever — held by the operator

AgDR-042's flock is checkout-scoped; the double-dispatch hazard is repo-scoped,
and that gap was #15/#56's territory. This record closes the gap by constraint
instead of mechanism: **the operator runs at most one orchestrator process per
repository, across all machines and checkouts.** The 2026-08-09 incident that
motivated AgDR-042 was self-inflicted duplication; the constraint makes it
unreachable by policy rather than by a ClaimStore.

#15 and #56 are iceboxed, not closed — they remain the only tickets owning the
mechanism-based answer. **Reopening condition:** the day the operator
deliberately runs a second checkout or second runner against one repo, this
constraint is void and #15 re-enters drafting *before* that second process
starts.

### 3. Observability investment goes to the read-only snapshot, and loopback is final

The operator's window into the system is #12's running-set surface
(SPEC.core §13.3 snapshot, §13.7 optional HTTP extension). Under one operator,
loopback-bind with no auth is not a v1 simplification — it is the **final**
design. #12 sheds its multi-tenant open questions.

### 4. The scaling axis is projects, not seats

A one-person studio grows by registering more projects, not adding users. The
investments that compound are project-onboarding ergonomics (#171/#172), the
cross-project snapshot (#12), and a per-project queue-steward ritual (#38/#39
rescope): a recurring session that verifies tickets against HEAD, refreshes
bodies via fold proposals, adjusts `blockedBy` edges, and posts an ordering
rationale.

### What explicitly does not change

The merge guard, Gate C, the stance ladder, and triage strictness all stand.
The threat model was never an untrusted colleague — it is agent error and
prompt injection, and a single operator has *more* riding on mechanical guards,
not less, because no second human reviews a merge.

## Rejected alternatives (steelmanned)

- **Install board-sync as a mirror (drags never honored).** Cheapest way to keep
  a glanceable board, and the code is merged and green. Rejected: it requires a
  PAT and a repo secret for a surface with no control role, adds a `*/5` cron to
  babysit, and #12 PR1 serves glanceability better. The mirror's one advantage —
  zero-setup viewing on github.com — does not outweigh a standing credential.
- **Delete the staged workflow and close #15/#56 outright.** Honest about
  intent. Rejected: both retirements are cheap to keep reversible, and #15/#56
  hold the only worked design for a hazard that has *already occurred once*.
  Close would discard the reopening tripwire along with the tickets.
- **Keep the multi-user posture "just in case".** The status quo. Rejected: it
  is not free — it has held two claiming tickets, a board pipeline, and an
  unmerged PR in the queue for months, and every triage round re-litigates
  them. An assumption that only costs is not an option, it is a debt.

## Blast radius

- **Queue:** #157 and PR #179 close; #15/#56 icebox. The active queue drops
  from 17 to 12 and every remaining ticket serves the single-operator model.
- **#167:** part (b)'s board-sync unification target evaporates; the
  `hold:parked` migration loses its forcing deadline ("before the Action goes
  live") and is re-prioritized on dispatch-clarity merit alone. Part (a)
  (explicit precedence) *gains* weight — labels are now the sole control plane.
- **#12:** the (a)-local-file vs (c)-HTTP decision narrows to §13.3/§13.7 as
  written; auth/tenancy questions are struck.
- **#56:** the runner-identity prerequisite (shared App login) is moot while
  iceboxed but must be restored to the body on any reopen.
- **Docs:** SETUP.md's board-sync `[MANUAL]` install stage and AgDR-009's
  pending permission-set record become historical notes, not open work.
- **AgDR-044** (sync arbitration) is not overturned — its logic stays merged
  and correct — but its operating premise is suspended with the board.

## Weakest point

**The load-bearing constraint is held by memory, not by a mechanism.** "One
orchestrator per repo" is enforced by nothing repo-scoped — AgDR-042's own
weakest point said exactly this, and this record converts that open hazard into
an accepted risk on the strength of operator discipline. The failure mode is
silent: a second machine, a forgotten launchd job after a restore, a stale
checkout — and the 2026-08-09 duplication replays with no ClaimStore to catch
it. The tripwire is one sentence in this file. If that feels thin in six
months, the cheap hardening is a repo-scoped advisory check (a lease comment on
a pinned ops issue at startup, warn-don't-block) — deliberately *not* specified
here to avoid rebuilding #56 by accident.

Second: retiring the board bets that #12 PR1 actually lands. Until it does, the
only running-state surface is `gh issue list` plus launchd logs — strictly
worse glanceability than even an unsynced board. Sequencing matters: the board
retirement is safe today because the board was never synced anyway, but the
observability debt is now on a named clock.

Third: single-operator means merges are ratified by one person on one screen.
The guards that backstop that (merge guard, Gate C) are now single points of
failure and their verification debt (#135's Codex asymmetry) is correspondingly
more urgent, not less.
