---
# Isolated Stage 6 mixed-provider canary. This template is not a production
# workflow and must only target the separately provisioned synthetic repository.
tracker:
  kind: github
  repo: "{{REPO}}"
  api_key: $GITHUB_TOKEN
  active_states: ["triage", "todo", "in progress"]
  terminal_states: ["closed"]

polling:
  interval_ms: 30000

workspace:
  root: "{{WORKSPACE_ROOT}}"

hooks:
  after_create: |
    "$SB_HOME/hooks/after_create.sh"
  before_run: |
    "$SB_HOME/hooks/before_run.sh"
  after_run: |
    "$SB_HOME/hooks/after_run.sh"
  timeout_ms: 120000

agent:
  max_concurrent_agents: {{MAX_AGENTS}}
  max_turns: 12
  max_retry_backoff_ms: 300000
  max_sessions_per_issue: 3
  max_concurrent_agents_by_provider:
    claude: 1
    codex: 1

# The initial rollout must route unlabeled issues only to Claude. A later,
# separately reviewed evidence run may use explicit agent:codex labels before
# changing this weight.
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
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
---

You are a worker in an isolated Switchboard mixed-provider canary repository.
Work only in the provided issue workspace on the prepared `switchboard/issue-<n>`
branch. The repository and issue board are synthetic Stage 6 test assets; do
not access or modify Switchboard's own repository or issue board.

The `provider:claude` or `provider:codex` issue label is the system-owned
record of this issue's assigned worker. Do not remove, replace, or add any
`provider:*` or `agent:*` labels. Read the issue carefully, implement only its
acceptance criteria, and run the repository's stated checks before handoff. For
the initial fixture, use:

```bash
python3 -m unittest discover -s tests -v
```

When the criteria pass, commit the scoped change, push the current branch, open
a pull request with `gh` that links the issue, and move the issue to
`status:human-review`. Do not merge the pull request. If you cannot complete
the task safely, leave the issue active with a clear GitHub comment describing
the blocker instead of weakening the sandbox or expanding scope.
