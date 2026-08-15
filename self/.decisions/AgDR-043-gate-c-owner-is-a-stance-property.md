# AgDR-043 — Gate C's owner is a stance property, and the merge guard must read it

- **Status:** accepted
- **Issue:** follow-up to #133 / AgDR-036
- **Date:** 2026-08-15

## Context

`AgDR-036` gave Gate C a mechanism: a PreToolUse hook denying an enumerated set
of Bash shapes, `gh pr merge` among them. It shipped denying that verb
**unconditionally**.

The same day, the `prototype` stance (`AgDR-039`) went live on civ-life, where a
QA session is dispatched to `status:review` and merges its own reviewed PRs. It
did exactly that twice, unattended.

Both are correct in isolation and contradict each other in composition. The
guard is orchestrator code shared by every project out of one installed runtime,
so merging #136 revoked civ-life's merge right — the autonomy the stance ladder
exists to provide — with no signal beyond a denied tool call. It had not yet
bitten only because that project's process was still running pre-#136 code.

## Decision

The merge guard denies `gh pr merge` **unless the project's stance dispatches an
agent to the handoff state**. Passed to the hook as an argv flag
(`--gate-c-owner=agent`); absence denies.

The predicate is **derived, not declared**:

```python
def agent_owns_gate_c(self) -> bool:
    state = self.handoff_label[len("status:"):].replace("-", " ").lower()
    return state in self.active_states
```

A gate in this system is defined by nothing dispatching it. So "who owns Gate C"
and "does the handoff target get dispatched" are the same question, asked of the
same two fields the scheduler already uses. A separate `allow_agent_merge` key
could disagree with the dispatcher, and then the guard and the scheduler would
hold different beliefs about the same project — which is the class of bug this
record exists to close, not a new instance of it.

## Two boundaries worth stating

**Only `merge` relaxes.** `gh pr review --approve`, `gh pr close`, and
force-pushes stay denied for every stance. Approval is the reviewer's act and
self-approval defeats it whoever holds the gate; closing is abandonment rather
than review; a force-push destroys history. None become safe because a project
chose an agent reviewer. This keeps the flag a *Gate C switch* rather than a
*trust-the-agent switch* — a distinction that erodes silently if the next
denied shape is relaxed "for consistency".

**The grant is `(agent, this repo)`, never `(agent)`.** The flag carries the
project's repository, and a merge naming any *other* repository is denied even
on an agent-owned gate — whether it names it with `-R/--repo` or with a PR URL
in place of the number.

This was a P1 on the PR, and the original design was wrong in a way worth
recording. A project-wide boolean looks sufficient right up until you notice
the credential is not project-wide: workers authenticate with a GitHub App
installation token that reaches *every* repo the installation covers. So
civ-life's stance — correctly configured, doing exactly what it was told —
would have granted its sessions the ability to merge straight through
Switchboard's human gate. No misconfiguration required.

The general form: **when a permission is granted by one scope and exercised
with a credential from a wider scope, the grant must name its own boundary.**
Deriving "who owns Gate C" from the stance was right; forgetting that the
answer is only meaningful *for one repository* was not.

**Omission denies.** `_write_guard_settings` defaults to the human gate,
`ClaudeRunner` defaults to `""`, a guard invoked with no flag denies, and an
agent flag carrying no repo denies too — nothing bounds that grant, so there is
nothing to check it against. An older settings file, a hand-run hook, or a
construction site nobody updated fails closed. The flag can only widen, never
narrow.

## Why argv and not an environment variable

`runner._build_env` returns `None` to mean "inherit the parent env as-is", and
that signal is load-bearing for every turn without an agent token. Threading a
variable through it would mean always materialising a dict. The hook *command*
is already composed by `_write_guard_settings`, is per-session by construction,
and is readable in the settings file.

## Blast radius

`guard.py` (flag + one condition), `runner.py` (constructor arg, settings
command), `runner_selector.py` (both `ClaudeRunner` sites), `types.py` (the
predicate). `CodexRunner` is untouched and still has no PreToolUse surface at
all — that is **#135**, unchanged by this.

## Weakest point (accepted)

**A prototype-stance project pointed at a repo something else depends on still
gets agent merges.** The repo check confines the grant to the project's *own*
repository; it cannot tell whether that repository deserved an agent gate. The
stance ladder assumes the operator sets the stance honestly, and branch
protection remains the mechanical backstop — as `AgDR-036` already said of the
`gh api …/merge` residual, which this change does not touch either: the API
form reaches the same endpoint with the same token and is not a `gh pr merge`
shape.

**And the guard is still soft.** Denials are fed back to the agent, which may
route around them. This changes who is permitted, not how strongly.

## What would make this wrong

If a project on an agent-owned gate ever merges something a human would have
stopped, the answer is not to re-tighten this flag — it is that the stance was
wrong for that project. The signal to watch is the ESCALATE-to-SHIP ratio from
`AgDR-040`: a QA that never escalates is not reviewing, and that is when an
agent-owned Gate C becomes a rubber stamp.
