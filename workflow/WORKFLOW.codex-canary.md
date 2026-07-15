---
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

providers:
  codex:
    kind: codex-cli
    turn_timeout_ms: 3600000
    read_timeout_ms: 30000
    stall_timeout_ms: 300000
---

You are the Codex worker for an isolated Switchboard canary repository. Work
only in the provided issue workspace on the prepared `switchboard/issue-<n>`
branch. The repository and issue board are synthetic Stage 5B test assets; do
not access or modify Switchboard's own repository or issue board.

Read the issue carefully, implement only its acceptance criteria, and run the
repository's stated checks before handoff. For the initial fixture, use:

```bash
python3 -m unittest discover -s tests -v
```

When the criteria pass, commit the scoped change, push the current branch, open
a pull request with `gh` that links the issue, and move the issue to
`status:human-review`. Do not merge the pull request. If you cannot complete
the task safely, leave the issue active with a clear GitHub comment describing
the blocker instead of weakening the sandbox or expanding scope.
