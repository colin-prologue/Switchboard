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
