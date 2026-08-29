# AgDR-036 — Gate C gets a mechanism: an enumerated Bash deny-list on the one PreToolUse hook

- **Status:** proposed (ratify or overturn at the merge gate)
- **Issue:** #133 (worker merge-guard; blocking finding 1 of #43's round-2 triage)
- **Amends:** AgDR-004 (permission posture) — its "Bash commands are not
  statically analyzed" v1 scope limit is now partial, not absolute
- **Touches:** `orchestrator/src/orchestrator/guard.py` (deny-list + docstring),
  `orchestrator/src/orchestrator/runner.py:48` (`GUARD_MATCHER`),
  `orchestrator/tests/test_merge_guard.py` (new)

## Context

Gate C — "a human merges, agents never self-merge" (METHODOLOGY.md) — was
enforced by prompt text and model compliance alone. The live worker allowlist
(`workflow/WORKFLOW.base.md:72`) grants `Bash(gh:*)` and `Bash(git:*)`, which
permit `gh pr merge`, `gh pr review --approve`, `gh pr close` and force-pushes;
`GUARD_MATCHER` did not include `Bash`, so the PreToolUse hook never fired for a
Bash call at all. The only thing standing between a worker and its own merge
button was the instruction not to press it.

## Premise VERIFIED (2026-08-16, issue #158)

This record's weakest point was that its load-bearing vendor premise had never
been checked: that a PreToolUse exit 2 denies a Bash call **even when
`--allowedTools "Bash(gh:*)"` grants it under `--permission-mode acceptEdits`**.
It assigned the check to the merge gate. PR #136 was merged without it, and two
further decisions were built on top before anyone noticed.

Run against `claude` 2.1.233, four cases, real CLI:

| Gate | Command | Result |
|---|---|---|
| human | `gh pr merge --help` (with `Bash(gh:*)` granted) | **DENIED** |
| human | `gh pr view --help` | allowed |
| agent, own repo | `gh pr merge --help` | allowed |
| agent, other repo | `gh pr merge -R <other> 12` | **DENIED** |

Hook evaluation precedes the allowlist. The guard is real, it denies the right
shapes rather than everything, and `AgDR-043`'s conditional relaxation and its
repo boundary both behave as designed end-to-end — not only in the unit tests,
which exercise `guard.py` in isolation and would have passed identically had the
hook never been consulted.

Two things worth keeping from how this went. The verification was assigned to a
human gate and simply did not happen; **an assignment reads as diligence and
produces nothing**. And running it surfaced a defect no unit test had: the
cross-repo refusal reused the human-gate wording ("Gate C is Colin's") on a
project whose gate is *not* a human's and whose own merges *are* permitted —
fixed in the same change.

Nothing here relaxes the second weakest point below: the guard remains **soft**.

## Amended by AgDR-043 (2026-08-15)

`gh pr merge` is no longer denied unconditionally. It is denied unless the
project's stance dispatches an agent to the handoff state, and then only for
merges targeting that project's own repository.

The composition this record missed: it was written while every project ran a
human Gate C, and the `prototype` stance (`AgDR-039`) shipped the same week with
a QA session that merges its own reviewed PRs. Merging this guard silently
revoked that, for every project at once, because the guard is orchestrator code
shared out of one installed runtime.

`gh pr review --approve`, `gh pr close`, and the force-push shapes below are
unchanged — denied under every stance. So is the `gh api …/pulls/{n}/merge`
residual named in this record: it is not a `gh pr merge` shape and reaches the
same endpoint with the same token.

Read the enumeration below as still accurate about *what* is denied, and
`AgDR-043` as the authority on *when* the merge verb is.

## Codex residual: mechanism UNVERIFIABLE here, closed by refusal (2026-08-29, issue #135)

The "Named residual — Codex" bullet below is now answered by `AgDR-2026-08-29-codex-has-no-guard-surface-so-dispatch-refuses`. Read
the bullet as still accurate about the *gap* — `CodexRunner` injects no settings
and has no hook surface — and `AgDR-2026-08-29-codex-has-no-guard-surface-so-dispatch-refuses` as the authority on what compensates
for it.

**The investigation this record's residual implied, and what it returned.** Issue
#135's first criterion was to establish whether Codex-CLI has a PreToolUse
equivalent. From inside a worker session the answer is *not determinable*, and
the three closed doors are worth naming so the next session does not re-walk
them:

- `codex` is not installed in the worker image (`which codex` → not found), and
  it is on no worker allowlist, so it cannot be interrogated even where present.
- Web search/fetch are not granted to worker sessions; both were denied.
- The project's own record of the Codex configuration surface —
  `spec/SPEC.core.md` §5.3.6 — enumerates `approval_policy`, `thread_sandbox`,
  and `turn_sandbox_policy`. Those are **approval and sandbox posture**, not a
  per-call veto: they decide whether a command needs approval and what the
  filesystem/network boundary is, not whether *this* `gh pr merge` is refused.
  The adapter's shipped default (`types.py`) already pins the permissive end of
  both (`--ask-for-approval never --sandbox workspace-write`). Nothing in the
  spec, the adapter, or the record base names a PreToolUse analogue.

`SPEC.core.md` also names the one command that would answer it definitively —
`codex app-server generate-json-schema` — and that command sits outside the
worker allowlist. So the shape of the situation is *exactly* the one this record
got wrong in round 1: a vendor premise checkable only at a gate.

**The design response is the lesson from the section above, applied.** What went
wrong in #133 was not that verification was deferred; it was that a *mechanism
whose correctness depended on the unverified premise* shipped anyway. `AgDR-2026-08-29-codex-has-no-guard-surface-so-dispatch-refuses`
therefore ships a mechanism that does not depend on the answer at all — a
dispatch-time refusal, entirely orchestrator-side, correct whichever way the
Codex question resolves. If a hook surface is later found, the refusal is a
conservative over-block to relax; if none exists, it is the only floor available.
An assignment to a gate reads as diligence and produces nothing — so this change
assigns nothing to one.

## Decision

1. **`Bash` joins the existing matcher** — `Bash|Write|Edit|MultiEdit|NotebookEdit`
   — rather than adding a second PreToolUse entry. One hook, one settings file;
   the containment guard's `FILE_PATH_KEYS` loop runs as a harmless no-op on Bash
   calls (Bash `tool_input` carries no path keys).

2. **An ENUMERATED deny-list, evaluated before workspace resolution.** The merge
   deny is workspace-INDEPENDENT and must not ride behind `guard.py`'s
   no-workspace early return, or a payload without `CLAUDE_PROJECT_DIR`/`cwd`
   silently allows `gh pr merge`. Denied shapes, exactly: `gh pr merge …`,
   `gh pr review … --approve`, `gh pr close …`, `git push --force`,
   `git push --force-with-lease`, `git push -f`, and the `+refspec` force form.
   Exit 2 with stderr prefixed `switchboard-guard: denied:` — a fixed grep anchor
   for operators — naming the matched shape and the handoff hint.

3. **Matching is by VERB POSITION, not token presence,** anchored at `tokens[0]`.
   A plain per-token scan cannot tell a subcommand from a quoted argument:
   `gh pr create --body "a human will merge"` tokenizes to gh∧pr∧merge and would
   DENY the MANDATORY handoff step (`WORKFLOW.base.md:339-341`), stranding every
   implementing session whose PR body mentions merging — this ticket's own PR
   first. The `gh` rules fire only when `tokens[0] == "gh"`; global flags may sit
   between it and `pr`, but that `pr` must be the command's own subcommand, and
   the denied verb must immediately follow it. The tokenizer is `shlex.split`, so
   a quoted body collapses to one token and `+refs/heads/x` stays one token;
   `ValueError` (unbalanced quote) falls back to `command.split()` — fail toward
   evaluation, never toward a silent allow.

4. **What this is, honestly.** A deny-list that RAISES THE COST of a Gate C
   violation and makes every attempt OBSERVABLE — not a security boundary.
   AgDR-004's addendum ratifies soft denial semantics: denials are fed back to
   the agent, which may route around them. `gh api -X PUT
   repos/{o}/{r}/pulls/{n}/merge` reaches the same endpoint with the same
   inherited token. Branch protection is the mechanical backstop for that
   residual, and is out of scope here.

## Rejected options, steelmanned

- **Narrow `--allowedTools` instead** (drop `Bash(gh:*)` for a list of permitted
  `gh` subcommands). This is the stronger mechanism — the permission system, not
  a soft hook — and it needs no parsing at all. Rejected because `--allowedTools`
  patterns are prefix-shaped: enumerating every permitted `gh`/`git` form a
  worker legitimately needs (view, comment, diff, create, issue …, checkout,
  commit, push, rev-parse …) is a large, drift-prone list whose first omission
  strands a session with no diagnostic. The guard fails the other way: its first
  omission lets something through, visibly. Worth revisiting if the deny-list
  proves load-bearing.
- **A general Bash static analyzer / shell parser.** Would close the
  compound-command and aliasing holes. It is the rabbit hole AgDR-004 named, and
  every increment of parser cleverness is a new way to deny a legitimate command.
  Explicit non-goal.
- **Splitting on `&&` / `;` / `|`** so `git status && gh pr merge 12` is caught.
  Rejected for the same reason as (3): a PR body describing "`git fetch && gh pr
  merge`" would strand the session that writes it. Reading `tokens[0]` correctly
  allows the compound form — a NAMED residual, consistent with the
  raises-the-cost-not-a-boundary posture.
- **Shape-matching `gh api …/pulls/{n}/merge`.** Catches the one bypass we know
  the name of, and invites the belief that the rest are covered. The endpoint
  surface is open-ended (GraphQL `mergePullRequest`, a `curl` the allowlist
  happens to admit); branch protection is the right backstop.
- **A second PreToolUse entry for `Bash`.** Cleaner separation of the two
  concerns. Two matchers, two settings entries, two places to keep in sync for a
  guard whose Bash branch is ~40 lines.

## Blast radius

- **Every Claude-CLI worker session, all roles, all projects:** the injected
  settings file now matches Bash. The containment loop no-ops there; the deny
  fires only on the enumerated shapes in verb position. Free-text arguments never
  match, and `gh pr create` (the handoff path) is a pinned ALLOW case.
- **The session transcript** carries the `switchboard-guard: denied:` string —
  the observability the ticket actually buys.
- `test_audit_fixes.py`'s existing guard tests are unaffected (the matcher change
  is additive; a test asserts the old matchers survive).
- **Named residual — Codex.** `CodexRunner` injects no settings and has no hook
  surface, and Codex is reachable on the real repo via label precedence
  (`runner_selector.py:52-67`) regardless of today's `codex: 0` weight. Filed as
  #135 (native `blockedBy` this issue). Round 1 is Claude-CLI only.

## Weakest point

The load-bearing vendor premise is unverified in this PR: that a PreToolUse exit
2 denies a Bash call **even when `--allowedTools "Bash(gh:*)"` grants it under
`--permission-mode acceptEdits`**. If hook evaluation does not precede or
override the allowlist, this ships a no-op with a passing test suite. It cannot
be checked from inside a worker session — `claude` is on no worker allowlist, and
a nested `claude -p` would be a recursive spawn on the session's own auth — so
verification is assigned to the human merge gate: one `claude -p` with the guard
settings plus the live allowlist, issuing `gh pr merge --help`, result recorded
on the PR. The in-allowlist proof obligation here is the `_run_guard` subprocess
assertions only.

Second: the guard is soft by construction (decision 4). It changes a violation
from free to conspicuous. It does not make one impossible, and should never be
cited as though it does.
