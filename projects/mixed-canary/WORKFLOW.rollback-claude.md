---
# Isolated rollback proof for the Stage 6 mixed-provider canary. This workflow
# deliberately contains only Claude and is launched with the default CLI mode.
tracker:
  kind: github
  repo: "colin-prologue/switchboard-mixed-canary"
  api_key: $GITHUB_TOKEN
  active_states: ["triage", "todo", "in progress"]
  terminal_states: ["closed"]

polling:
  interval_ms: 30000

workspace:
  root: "/Users/colindwan/Developer/switchboard-workspaces/mixed-canary"

hooks:
  after_create: |
    "$SB_HOME/hooks/after_create.sh"
  before_run: |
    "$SB_HOME/hooks/before_run.sh"
  after_run: |
    "$SB_HOME/hooks/after_run.sh"
  timeout_ms: 120000

agent:
  max_concurrent_agents: 1
  max_turns: 12
  max_retry_backoff_ms: 300000
  max_sessions_per_issue: 3

providers:
  claude:
    kind: claude-cli
    command: "claude -p --verbose --output-format stream-json --permission-mode acceptEdits --allowedTools \"Bash(git:*)\" \"Bash(gh:*)\" \"Bash(python3 -m unittest:*)\""
    max_turns: 12
    max_budget_usd: 5
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
---

You are the Claude worker for the isolated Switchboard rollback canary. Work
only in the provided issue workspace on the prepared `switchboard/issue-<n>`
branch. This repository and issue board are synthetic Stage 6 test assets; do
not access or modify Switchboard's own repository or issue board.

The existing `provider:codex` label is deliberate rollback evidence. Do not
remove, replace, or add any `provider:*` or `agent:*` labels. Implement only the
issue's acceptance criteria and run:

```bash
python3 -m unittest discover -s tests -v
```

When the criteria pass, commit the scoped change, push the current branch, open
a pull request whose body closes the issue, and move the issue to
`status:human-review`. Do not merge the pull request. If blocked, leave a clear
issue comment instead of weakening the sandbox or expanding scope.
