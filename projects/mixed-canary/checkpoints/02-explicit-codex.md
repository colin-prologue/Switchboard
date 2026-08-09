## Intent

Prove that an explicit `agent:codex` request receives a durable Codex assignment
and completes through the same mixed-provider process.

## Acceptance criteria

- Add `cheer(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Go, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged. As your FINAL
  action write the git-excluded `.run/handoff-evidence.json` (issue #61):
  `{"issue": "<this issue's number>", "pr_number": <PR number>, "head_sha": "<git rev-parse HEAD>"}`
  Do NOT change issue labels — the orchestrator owns the `status:human-review`
  transition. Do not merge it.

## Non-goals

- Do not change dependencies, tooling, labels, or unrelated fixture behavior.
