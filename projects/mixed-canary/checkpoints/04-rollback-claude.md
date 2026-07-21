## Intent

Prove the operational rollback: the default Claude-only process dispatches with
Claude even when the synthetic issue retains a `provider:codex` audit label.

## Acceptance criteria

- Add `salutation(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Greetings, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged, and move this
  issue to `status:human-review`. Do not merge it.

## Non-goals

- Do not alter the existing provider label or use mixed mode for this checkpoint.
