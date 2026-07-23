---
# Inert Stage 7 circuit-recovery proof. The Codex command injects one typed
# availability failure in the issue workspace, then delegates the half-open
# recovery probe to the real subscription-authenticated Codex CLI.
tracker:
  kind: github
  repo: "colin-prologue/switchboard-mixed-canary"
  api_key: $GITHUB_TOKEN
  active_states: ["triage", "todo", "in progress"]
  terminal_states: ["closed"]

polling:
  interval_ms: 5000

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
  max_concurrent_agents_by_provider:
    claude: 1
    codex: 1

routing:
  weights:
    claude: 100
    codex: 0

providers:
  claude:
    kind: claude-cli
    command: "claude -p --verbose --output-format stream-json --permission-mode acceptEdits --allowedTools \"Bash(git:*)\" \"Bash(gh:*)\" \"Bash(python3 -m unittest:*)\""
    max_turns: 12
    max_budget_usd: 5
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
  codex:
    kind: codex-cli
    command: "/Users/colindwan/Developer/Switchboard/scripts/codex-circuit-canary.sh"
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
---

You are the recovery probe for the isolated Switchboard Stage 7 circuit canary.
Work only in the provided issue workspace on the prepared
`switchboard/issue-<n>` branch. The repository and issue board are synthetic;
do not access or modify Switchboard's own repository or issue board.

The `provider:codex` label is the system-owned assignment. Do not remove,
replace, or add any `provider:*` or `agent:*` labels. Read the issue carefully,
implement only its acceptance criteria, and run:

```bash
python3 -m unittest discover -s tests -v
```

When the criteria pass, commit the scoped change, push the current branch, open
a pull request whose body closes the issue, and move the issue to
`status:human-review`. Do not merge the pull request. If blocked, leave the
issue active with a clear comment instead of weakening the sandbox or expanding
scope.
