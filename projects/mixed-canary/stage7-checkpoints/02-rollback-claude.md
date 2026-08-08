## Intent

Repeat the unchanged operational rollback after the circuit canary: the
default Claude-only process must dispatch with Claude while the synthetic issue
retains a `provider:codex` audit label.

## Acceptance criteria

- Add `assurance(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Assured, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged. As your FINAL
  action write the git-excluded `.run/handoff-evidence.json` (issue #61):
  `{"issue": "<this issue's number>", "pr_number": <PR number>, "head_sha": "<git rev-parse HEAD>"}`
  Do NOT change issue labels — the orchestrator owns the `status:human-review`
  transition. Do not merge it.

## Non-goals

- Do not alter the existing provider label or use mixed mode for this checkpoint.
