## Intent

Prove that an explicit `agent:claude` request receives a durable Claude
assignment and completes through the mixed-provider process.

## Acceptance criteria

- Add `farewell(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Goodbye, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged, and move this
  issue to `status:human-review`. Do not merge it.

## Non-goals

- Do not change dependencies, tooling, labels, or unrelated fixture behavior.
