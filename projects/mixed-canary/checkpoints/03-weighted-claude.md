## Intent

Prove that an issue with no `agent:*` override routes to Claude while the mixed
workflow remains at `claude: 100, codex: 0`.

## Acceptance criteria

- Add `welcome(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Welcome, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged. As your FINAL
  action write the git-excluded `.run/handoff-evidence.json` (issue #61):
  `{"issue": "<this issue's number>", "pr_number": <PR number>, "head_sha": "<git rev-parse HEAD>"}`
  Do NOT change issue labels — the orchestrator owns the `status:human-review`
  transition. Do not merge it.

## Non-goals

- Do not add an agent override or change routing weights, dependencies, or tools.
