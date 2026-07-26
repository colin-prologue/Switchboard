## Intent

Prove the first nonzero automatic Codex route in the isolated mixed canary. The
issue has no `agent:*` override, and the dedicated evidence workflow uses
`claude: 0, codex: 100` so one reviewed ticket deterministically selects Codex.

## Acceptance criteria

- Add `acknowledgement(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Acknowledged, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged, and move this
  issue to `status:human-review`. Do not merge it.

## Non-goals

- Do not add or alter any `agent:*` or `provider:*` label.
- Do not treat the evidence workflow's `0/100` weights as an operating ratio or
  modify the checked-in `100/0` baseline workflow.
