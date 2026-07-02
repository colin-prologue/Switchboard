---
# Shared methodology base. register-project.sh composes a per-project WORKFLOW.md
# by substituting the ALL-CAPS placeholders below. Symphony loads ONE WORKFLOW.md per
# process from the project binding; project-specific values are filled at scaffold
# time, while the prompt body references shared, repo-owned methodology at runtime
# (reference-don't-inline, one level up).

tracker:
  kind: github
  repo: "colin-prologue/Switchboard"
  api_key: $GITHUB_TOKEN
  active_states: ["todo", "in progress"]
  terminal_states: ["done", "closed", "cancelled"]

polling:
  interval_ms: 30000

workspace:
  # Per-project root: GitHub issue numbers collide across repos, so namespace by slug.
  root: "/Users/colindwan/Developer/switchboard-workspaces/switchboard-self"

hooks:
  # Hooks run with cwd == the per-issue workspace dir. They derive the issue
  # number from the dir name and the repo/base from the exported project.env.
  after_create: |
    "$SB_HOME/hooks/after_create.sh"
  before_run: |
    "$SB_HOME/hooks/before_run.sh"
  after_run: |
    "$SB_HOME/hooks/after_run.sh"
  timeout_ms: 120000

agent:
  max_concurrent_agents: 4
  max_turns: 20
  max_retry_backoff_ms: 300000
  # Owned extension (spec/SPEC.md §4): worker sessions allowed per issue per
  # process lifetime before the orchestrator parks the issue (one notification
  # comment, workspace + logs preserved, no re-dispatch until the issue is
  # updated by a human). Caps are diagnostic checkpoints, not kill switches.
  max_sessions_per_issue: 3

# Pass-through execution block for the Claude adapter (see spec/SPEC.md §1).
# --verbose is required by the CLI for stream-json in -p mode. Documented
# permission posture (core §10.5): file edits auto-accepted (bounded by the
# runner-injected PreToolUse workspace-containment guard); git/gh commands
# allowed; everything else falls to the non-interactive default — the denial
# surfaces to the agent, and a session that cannot finish because of it ends
# in a non-success result, which fails the attempt (user-input-required is
# never left stalling).
claude:
  command: "claude -p --verbose --output-format stream-json --permission-mode acceptEdits --allowedTools \"Bash(git:*)\" \"Bash(gh:*)\""
  max_turns: 20
  max_budget_usd: 5
  turn_timeout_ms: 3600000
  read_timeout_ms: 5000
  stall_timeout_ms: 300000
---

You are a Switchboard engineering agent working a single GitHub issue from the
repository `colin-prologue/Switchboard`. Your workspace is already a clean clone of that repo,
checked out on branch `switchboard/issue-{{ issue.identifier }}` (the
before_run hook prepared it). Run only inside this workspace.

## The issue

- **{{ issue.identifier }}: {{ issue.title }}**
- URL: {{ issue.url }}

{{ issue.description }}

## How to work it

1. **Read the methodology first.** Open `METHODOLOGY.md` at the repo root of this
   workspace (or `methodology/METHODOLOGY.md`) and follow the gate-state workflow
   it defines. If it is absent, treat this as a Symphony-light ticket: implement,
   open a PR, hand off to review.
2. **Load product intent if referenced.** If the issue body contains a
   `parent-intent: <slug>` line, read `self/.switchboard/intents/<slug>.md`
   and treat its constraints (NFRs, environment, failure-branch policy) as binding.
   Do not re-derive or inline them.
3. **Honor the contract in the issue body.** The acceptance criteria are your
   definition of done and the non-goals are hard boundaries. Do not exceed scope.
4. **Implement** on the current branch. Keep commits scoped and conventional.
5. **Verify** against the acceptance criteria before handing off. Run the repo's
   checks/tests. Do not hand off red.
6. **Hand off, don't self-merge.** Commit, push the branch, open a PR with `gh`
   linking this issue, attach evidence of the criteria passing, then move the
   issue's `status:` label to the handoff state defined in `METHODOLOGY.md`
   (default `status:human-review`). Stop there. A human merges.

<!-- PHASE 4: before choosing any architecture, query the decision-corpus MCP for
relevant prior ADRs, and record a new ADR into self/.decisions/ whose
"forces" are the product-intent constraints. Enable once the corpus tool is installed. -->
