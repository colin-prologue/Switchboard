## Intent

Prove that an explicit `agent:codex` request receives a durable Codex assignment
and completes through the same mixed-provider process.

## Acceptance criteria

- Add `cheer(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Go, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged, and move this
  issue to `status:human-review`. Do not merge it.

## Non-goals

- Do not change dependencies, tooling, labels, or unrelated fixture behavior.
