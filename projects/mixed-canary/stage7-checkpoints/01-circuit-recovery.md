## Intent

Prove the Stage 7 provider circuit against a deterministic synthetic outage.
The first Codex invocation emits a typed `service_unavailable` result. After
the fixed cooldown, the sole half-open probe delegates to the real Codex CLI
and completes this issue without burning retry or session allowance.

## Acceptance criteria

- Add `confirmation(name: str) -> str` to `greeting.py`.
- Trim surrounding whitespace and return `Confirmed, <name>!`.
- Add focused unittest coverage in `tests/test_greeting.py`.
- `python3 -m unittest discover -s tests -v` passes.
- Commit and push only the scoped fixture change.
- Open a pull request whose body closes this issue when merged, and move this
  issue to `status:human-review`. Do not merge it.

## Non-goals

- Do not alter provider labels, routing weights, dependencies, or tooling.
- Do not bypass or shorten the fixed circuit cooldown.
